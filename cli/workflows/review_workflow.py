from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from github.File import File
from github.PullRequest import PullRequest

from ..anchor_engine import build_anchor_maps
from ..codex_client import CodexClient
from ..config import ReviewConfig, make_debug
from ..context_manager import ContextManager
from ..exceptions import CodexExecutionError
from ..github_client import GitHubClient, GitHubClientProtocol
from ..models import (
    PRIOR_FINDINGS_RECONCILIATION_SCHEMA,
    REVIEW_OUTPUT_SCHEMA,
    ExistingReviewComment,
    OpenCodexFindingsStats,
    PriorCodexFinding,
    ReviewRunResult,
)
from ..review.dedupe import (
    SUMMARY_MARKER,
    collect_existing_comment_texts,
    collect_existing_comment_texts_from_prior_findings,
    collect_existing_review_comments,
    collect_existing_review_comments_from_prior_findings,
    extract_prior_codex_findings,
    has_prior_codex_review,
    parse_priority_tag,
    prefilter_duplicates_by_location,
    summarize_prior_codex_findings,
)
from ..review.posting import (
    build_inline_comment_payloads,
    persist_anchor_maps,
    post_inline_comments,
)
from ..review_prompt import PromptBuilder

SUMMARY_TIP = (
    'Tip: comment with "/codex address comments" to attempt automated fixes for unresolved '
    "review threads."
)


def _build_review_summary(result: ReviewRunResult) -> str:
    return _build_review_summary_with_open_counts(
        result=result,
        new_findings_count=len(result.findings),
        open_findings=OpenCodexFindingsStats(),
    )


def _build_review_summary_with_open_counts(
    *,
    result: ReviewRunResult,
    new_findings_count: int,
    open_findings: OpenCodexFindingsStats,
) -> str:
    applicable_total = "unknown" if open_findings.unknown else str(open_findings.total)
    applicable_blocking = "unknown" if open_findings.unknown else str(open_findings.blocking)
    summary_lines = [
        SUMMARY_MARKER,
        f"- Overall: {result.overall_correctness.strip() or 'patch is correct'}",
        f"- Findings (new): {new_findings_count}",
        f"- Findings (applicable prior): {applicable_total}",
        f"- Applicable prior blocking findings (P0/P1): {applicable_blocking}",
    ]

    if not open_findings.unknown and open_findings.highest_priority is not None:
        summary_lines.append(
            f"- Highest applicable prior priority: P{open_findings.highest_priority}"
        )

    overall_explanation = result.overall_explanation.strip()
    if overall_explanation:
        summary_lines.append("")
        summary_lines.append(overall_explanation)

    summary_lines.append("")
    summary_lines.append(SUMMARY_TIP)
    return "\n".join(summary_lines)


def _canonical_overall_correctness(value: str) -> str:
    return (
        "patch is incorrect"
        if value.strip().lower().rstrip(".") == "patch is incorrect"
        else "patch is correct"
    )


def _resolve_finding_priority(finding: dict[str, Any]) -> int | None:
    priority_value = finding.get("priority")
    if isinstance(priority_value, int) and 0 <= priority_value <= 3:
        return priority_value

    title_value = finding.get("title")
    if isinstance(title_value, str):
        return parse_priority_tag(title_value)
    return None


def _has_blocking_findings(findings: list[dict[str, Any]]) -> bool:
    for finding in findings:
        priority = _resolve_finding_priority(finding)
        if priority in {0, 1}:
            return True
    return False


def _compute_effective_review_result(
    result: ReviewRunResult,
    finalized_findings: list[dict[str, Any]],
    applicable_prior_findings: OpenCodexFindingsStats,
) -> ReviewRunResult:
    base_overall = _canonical_overall_correctness(result.overall_correctness)
    has_new_blocking = _has_blocking_findings(finalized_findings)
    has_applicable_blocking = (
        not applicable_prior_findings.unknown
    ) and applicable_prior_findings.blocking > 0
    overall = (
        "patch is incorrect"
        if base_overall == "patch is incorrect" or has_new_blocking or has_applicable_blocking
        else "patch is correct"
    )

    notes: list[str] = []
    if has_applicable_blocking:
        notes.append(
            "Applicable prior Codex blocking findings (P0/P1) remain unresolved by current code."
        )
    if applicable_prior_findings.unknown:
        notes.append(
            "Applicable prior finding count is unavailable because reconciliation did not complete."
        )

    explanation = result.overall_explanation.strip()
    if notes:
        notes_text = " ".join(notes)
        explanation = f"{explanation} {notes_text}".strip() if explanation else notes_text

    return ReviewRunResult(
        overall_correctness=overall,
        overall_explanation=explanation,
        findings=list(finalized_findings),
    )


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _build_reconciliation_prompt(
    prior_findings: list[PriorCodexFinding],
    new_findings: list[dict[str, Any]],
) -> str:
    lines = [
        "<reconciliation_task>",
        "Determine which prior Codex findings are still applicable at the current PR head.",
        "A prior finding is NOT applicable if the code now addresses it, even when the GH thread is not manually closed.",
        "Use repository inspection and git history as needed.",
        "</reconciliation_task>",
        "<new_findings>",
        f"count={len(new_findings)}",
    ]
    for finding in new_findings:
        title = str(finding.get("title") or "").strip()
        if title:
            lines.append(title)
    lines.append("</new_findings>")
    lines.append("<prior_findings>")
    for prior in prior_findings:
        lines.append(
            f'<finding id="{prior.id}" priority="P{prior.priority}" '
            f'resolved="{str(prior.is_resolved).lower()}" '
            f'path="{prior.path}" line="{prior.line}">'
        )
        lines.append("<title>")
        lines.append(prior.title)
        lines.append("</title>")
        lines.append("<body>")
        lines.append(prior.body)
        lines.append("</body>")
        lines.append("</finding>")
    lines.append("</prior_findings>")
    lines.append(
        "Return only finding ids that are still applicable in applicable_prior_ids. "
        "Use dismissed_prior_ids for findings that are now addressed."
    )
    return "\n".join(lines)


class ReviewWorkflow:
    """Main workflow for code review operations."""

    def __init__(
        self,
        config: ReviewConfig,
        *,
        github_client: GitHubClientProtocol | None = None,
        codex_client: CodexClient | None = None,
    ) -> None:
        self.config = config
        self.prompt_builder = PromptBuilder(config)
        self.codex_client = codex_client or CodexClient(config)
        self.context_manager = ContextManager()
        self.github_client: GitHubClientProtocol = github_client or GitHubClient(config)
        self._debug = make_debug(config)

    def _build_review_base_instructions(self, guidelines: str) -> str:
        """Construct base instructions for Codex review runs."""
        parts: list[str] = [
            "You are an autonomous code review assistant.",
            "Follow the review guidelines below verbatim while producing prioritized, actionable findings.",
            "Treat 'REVIEW COMMENT FORMAT (REPO STANDARD)' as authoritative over generic formatting guidance.",
        ]

        guidelines_text = guidelines.strip()
        if guidelines_text:
            parts.append("\nReview guidelines:\n" + guidelines_text)

        parts.append(
            "Use git commands as needed to inspect the diff between the PR head and the base branch."
        )
        return "\n".join(parts).strip()

    def _build_schema_prompt(
        self,
        existing_texts: list[str],
        prior_findings_count: int,
    ) -> str:
        """Build turn-2 prompt for structured output with prior-finding context."""
        lines: list[str] = [
            "<prior_codex_findings>",
            f"count={prior_findings_count}",
            "</prior_codex_findings>",
        ]

        if not existing_texts:
            lines.append(
                "Produce the JSON review output now. Return only newly introduced findings in this diff."
            )
            return "\n".join(lines)

        lines.append("<existing_review_comments>")
        for text in existing_texts[:200]:
            lines.append(text)
        lines.append("</existing_review_comments>")
        lines.append(
            "Produce the JSON review output now. "
            "Exclude any findings that are semantically redundant with the existing review comments above. "
            "Treat prior_codex_findings as previously reported findings (resolved or unresolved) "
            "and only return newly introduced findings."
        )
        return "\n".join(lines)

    def process_review(self, pr_number: int | None = None) -> dict[str, Any]:
        """Process a code review for the given pull request."""
        resolved_pr_number = pr_number if pr_number is not None else self.config.pr_number
        if resolved_pr_number is None:
            raise ValueError("PR number must be provided")

        self._debug(1, f"Processing review for {self.config.repository} PR #{resolved_pr_number}")

        pr = self.github_client.get_pr(resolved_pr_number)
        if not isinstance(pr, PullRequest):
            raise ValueError("Expected a PullRequest instance")

        changed_files = list(pr.get_files())
        rename_map: dict[str, str] = {}
        for changed_file in changed_files:
            if changed_file.status == "renamed":
                previous_filename = changed_file.previous_filename
                if previous_filename:
                    rename_map[previous_filename] = changed_file.filename

        head_sha = pr.head.sha if pr.head else None
        if not head_sha:
            raise ValueError("Missing head commit SHA")

        self._debug(1, f"Changed files: {len(changed_files)}")
        for changed_file in changed_files[:10]:
            patch_len = (
                len(changed_file.patch.splitlines()) if isinstance(changed_file.patch, str) else 0
            )
            self._debug(
                2,
                f" - {changed_file.filename} status={changed_file.status} patch_len={patch_len}",
            )

        repo_root = self.config.repo_root or Path(".").resolve()
        context_dir_name = self.config.context_dir_name or ".codex-context"
        self.context_manager.write_context_artifacts(pr, repo_root, context_dir_name)

        guidelines = self.prompt_builder.load_guidelines()
        raw_prompt = self.prompt_builder.compose_prompt(changed_files, pr)
        base_instructions = self._build_review_base_instructions(guidelines)
        prompt = base_instructions + "\n\n" + raw_prompt

        self._debug(1, f"Final Review Prompt:\n{prompt}")
        self._debug(2, f"Prompt length: {len(prompt)} chars")

        # Fetch existing comments before execute() so we can feed them
        # into the structured-output turn for inline deduplication.
        review_comments_snapshot = list(pr.get_review_comments())
        issue_comments_snapshot = list(pr.get_issue_comments())
        reviews_snapshot = list(pr.get_reviews())
        had_prior_codex_review = has_prior_codex_review(reviews_snapshot, issue_comments_snapshot)
        prior_codex_findings: list[PriorCodexFinding] = []
        applicable_prior_findings = OpenCodexFindingsStats()
        dedupe_texts: list[str] = []
        existing_struct_for_prefilter: list[ExistingReviewComment] = []
        prior_findings_fetch_failed = False

        try:
            review_threads = self.github_client.get_review_threads(pr)
            prior_codex_findings = extract_prior_codex_findings(review_threads)
            dedupe_texts = collect_existing_comment_texts_from_prior_findings(prior_codex_findings)
            existing_struct_for_prefilter = collect_existing_review_comments_from_prior_findings(
                prior_codex_findings
            )
            self._debug(1, f"Prior Codex findings loaded: {len(prior_codex_findings)}")
        except Exception as exc:
            prior_findings_fetch_failed = True
            self._debug(
                1,
                f"Failed to retrieve review threads for prior-finding context: {exc}",
            )
            applicable_prior_findings = OpenCodexFindingsStats.unknown_stats()
            if had_prior_codex_review:
                dedupe_texts = collect_existing_comment_texts(review_comments_snapshot)
                existing_struct_for_prefilter = collect_existing_review_comments(
                    review_comments_snapshot
                )

        schema_prompt = self._build_schema_prompt(
            dedupe_texts,
            len(prior_codex_findings),
        )

        print("Running Codex to generate review findings...", flush=True)

        output = self.codex_client.execute(
            prompt,
            sandbox_mode="danger-full-access",
            output_schema=REVIEW_OUTPUT_SCHEMA,
            schema_prompt=schema_prompt,
        )

        try:
            payload = json.loads(output)
        except json.JSONDecodeError as parse_err:
            self._debug(1, f"Structured output was not valid JSON: {parse_err}")
            print("Model did not return valid JSON:")
            print(output)
            raise CodexExecutionError(f"JSON parsing error: {parse_err}") from parse_err

        parsed_result = ReviewRunResult.from_payload(payload)
        finalized_findings = self._finalize_findings(
            findings=parsed_result.findings,
            rename_map=rename_map,
            existing_struct=existing_struct_for_prefilter,
        )
        if not prior_findings_fetch_failed and prior_codex_findings:
            try:
                applicable_prior_ids = self._reconcile_applicable_prior_finding_ids(
                    prior_codex_findings,
                    finalized_findings,
                )
                applicable_prior_id_set = set(applicable_prior_ids)
                applicable_prior_list = [
                    finding
                    for finding in prior_codex_findings
                    if finding.id in applicable_prior_id_set
                ]
                applicable_prior_findings = summarize_prior_codex_findings(applicable_prior_list)
                self._debug(
                    1,
                    "Applicable prior Codex findings after reconciliation: "
                    f"{applicable_prior_findings.total}",
                )
            except Exception as exc:
                self._debug(1, f"Failed to reconcile prior findings applicability: {exc}")
                applicable_prior_findings = OpenCodexFindingsStats.unknown_stats()

        effective_result = _compute_effective_review_result(
            parsed_result,
            finalized_findings,
            applicable_prior_findings,
        )
        summary = _build_review_summary_with_open_counts(
            result=effective_result,
            new_findings_count=len(finalized_findings),
            open_findings=applicable_prior_findings,
        )
        if not self.config.dry_run:
            self._delete_prior_summary(pr)
            pr.as_issue().create_comment(summary)
        else:
            self._debug(1, "DRY_RUN: would refresh summary issue comment")

        self._post_results(
            effective_result,
            changed_files,
            pr,
            head_sha,
            rename_map,
        )
        return effective_result.as_dict()

    def _reconcile_applicable_prior_finding_ids(
        self,
        prior_codex_findings: list[PriorCodexFinding],
        new_findings: list[dict[str, Any]],
    ) -> list[str]:
        prompt = _build_reconciliation_prompt(prior_codex_findings, new_findings)
        reconciliation_output = self.codex_client.execute(
            prompt,
            suppress_stream=True,
            sandbox_mode="danger-full-access",
            output_schema=PRIOR_FINDINGS_RECONCILIATION_SCHEMA,
            schema_prompt="Return the reconciliation JSON now.",
        )
        payload = json.loads(reconciliation_output)
        applicable_ids = _as_string_list(payload.get("applicable_prior_ids"))
        valid_ids = {finding.id for finding in prior_codex_findings}
        return [finding_id for finding_id in applicable_ids if finding_id in valid_ids]

    def _finalize_findings(
        self,
        *,
        findings: list[dict[str, Any]],
        rename_map: dict[str, str],
        existing_struct: list[ExistingReviewComment],
    ) -> list[dict[str, Any]]:
        finalized = list(findings)

        if not existing_struct:
            return finalized

        before_prefilter = len(finalized)
        finalized = prefilter_duplicates_by_location(
            finalized,
            existing_struct,
            rename_map,
            self.config.repo_root or Path(".").resolve(),
        )
        dropped_prefilter = before_prefilter - len(finalized)
        if dropped_prefilter > 0:
            print(
                "Prefilter dropped "
                f"{dropped_prefilter}/{before_prefilter} findings due to existing comments"
            )
        return finalized

    def _post_results(
        self,
        result: ReviewRunResult | dict[str, Any],
        changed_files: list[File],
        pr: PullRequest,
        head_sha: str,
        rename_map: dict[str, str],
    ) -> None:
        """Post review results to GitHub."""
        normalized = (
            result if isinstance(result, ReviewRunResult) else ReviewRunResult.from_payload(result)
        )
        findings: list[dict[str, Any]] = list(normalized.findings)

        file_maps = build_anchor_maps(changed_files)
        repo_root = self.config.repo_root or Path(".").resolve()
        persist_anchor_maps(file_maps, repo_root, self.config.context_dir_name or ".codex-context")

        payloads = build_inline_comment_payloads(
            findings,
            file_maps,
            rename_map,
            repo_root,
            dry_run=self.config.dry_run,
            debug=self._debug,
        )
        post_inline_comments(
            pr,
            head_sha,
            payloads,
            dry_run=self.config.dry_run,
            debug=self._debug,
        )

    def _delete_prior_summary(self, pr: PullRequest) -> None:
        """Delete prior Codex summary issue comments."""
        comments = list(pr.get_issue_comments())
        for comment in comments:
            comment_body = comment.body
            body = comment_body.strip() if isinstance(comment_body, str) else ""
            if SUMMARY_MARKER not in body:
                continue
            try:
                comment.delete()
                self._debug(1, f"Deleted prior summary issue comment id={comment.id}")
            except Exception as exc:
                self._debug(
                    1, f"Failed to delete prior summary issue comment id={comment.id}: {exc}"
                )
