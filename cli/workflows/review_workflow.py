from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..clients.codex_client import CodexClient
from ..clients.git_ops import git_commit_shas, git_diff_text, git_is_ancestor
from ..clients.github_client import GitHubClient, GitHubClientProtocol
from ..core.config import ReviewConfig, make_debug
from ..core.exceptions import CodexExecutionError, ReviewContractError
from ..core.github_types import (
    ChangedFileProtocol,
    IssueCommentLikeProtocol,
    PullRequestLikeProtocol,
    ReviewCommentLikeProtocol,
)
from ..core.models import (
    REVIEW_OUTPUT_SCHEMA,
    CarriedForwardReviewComment,
    PriorCodexReviewComment,
    ReviewRunResult,
)
from ..review.anchor_engine import build_anchor_maps
from ..review.artifacts import ReviewArtifacts
from ..review.context_manager import ReviewContextWriter
from ..review.dedupe import (
    SUMMARY_MARKER,
    collect_codex_author_logins,
    collect_prior_codex_review_comments,
    render_prior_codex_comments_for_prompt,
)
from ..review.posting import (
    ReviewPostingOutcome,
    build_inline_comment_payloads,
    persist_anchor_maps,
    post_inline_comments,
)
from ..review.resume_state import (
    MAX_INLINE_INCREMENTAL_DIFF_LINES,
    load_latest_thread_id,
    parse_reviewed_head_sha,
    render_review_summary_metadata,
)
from ..review.review_prompt import compose_prompt, load_guidelines

SUMMARY_TIP = (
    'Tip: comment with "/codex address comments" to attempt automated fixes for unresolved '
    "review threads."
)


@dataclass(frozen=True)
class _ReviewSnapshots:
    review_comments: list[ReviewCommentLikeProtocol]
    issue_comments: list[IssueCommentLikeProtocol]
    prior_codex_comments: list[PriorCodexReviewComment]


@dataclass(frozen=True)
class ThreadResolutionOutcome:
    resolved_count: int
    failed_count: int
    dry_run: bool = False

    @classmethod
    def empty(cls, *, dry_run: bool) -> ThreadResolutionOutcome:
        return cls(resolved_count=0, failed_count=0, dry_run=dry_run)


@dataclass(frozen=True)
class ReviewSummary:
    overall_correctness: str
    current_findings_count: int
    carried_forward_count: int
    active_findings_count: int
    resolved_count: int
    resolution_failure_count: int


@dataclass(frozen=True)
class ReviewWorkflowResult:
    review: ReviewRunResult
    posting_outcome: ReviewPostingOutcome
    resolution_outcome: ThreadResolutionOutcome
    summary: ReviewSummary


@dataclass(frozen=True)
class _ReviewResumeState:
    previous_reviewed_sha: str
    resume_thread_id: str
    inline_diff: str | None
    commit_shas: tuple[str, ...]


def _build_review_summary(
    review: ReviewRunResult,
    summary: ReviewSummary,
    posting_outcome: ReviewPostingOutcome,
    resolution_outcome: ThreadResolutionOutcome,
    *,
    reviewed_head_sha: str,
) -> str:
    resolved_label = (
        "Prior Codex findings ready to resolve"
        if resolution_outcome.dry_run
        else "Prior Codex findings auto-resolved"
    )
    summary_lines = [
        SUMMARY_MARKER,
        render_review_summary_metadata(reviewed_head_sha),
        f"- Overall: {summary.overall_correctness.strip() or 'patch is correct'}",
        f"- New findings this run: {summary.current_findings_count}",
        f"- Prior unresolved Codex findings still relevant: {summary.carried_forward_count}",
        f"- {resolved_label}: {summary.resolved_count}",
        f"- Active findings total: {summary.active_findings_count}",
    ]
    if summary.resolution_failure_count > 0:
        summary_lines.append(
            f"- Prior Codex thread resolution failures: {summary.resolution_failure_count}"
        )
    if posting_outcome.dropped_count > 0:
        summary_lines.append(
            f"- Findings not publishable: {posting_outcome.dropped_count} ({posting_outcome.describe_drops()})"
        )
    if posting_outcome.post_result.dry_run:
        summary_lines.append(f"- Inline comments ready: {posting_outcome.publishable_count}")

    overall_explanation = review.overall_explanation.strip()
    if overall_explanation:
        summary_lines.append("")
        summary_lines.append(overall_explanation)
    if summary.carried_forward_count > 0:
        noun = "finding" if summary.carried_forward_count == 1 else "findings"
        verb = "was" if summary.carried_forward_count == 1 else "were"
        summary_lines.append("")
        summary_lines.append(
            f"{summary.carried_forward_count} prior unresolved Codex {noun} "
            f"{verb} re-adjudicated as still relevant."
        )
    if summary.resolved_count > 0:
        noun = "finding" if summary.resolved_count == 1 else "findings"
        verb = "is" if resolution_outcome.dry_run and summary.resolved_count == 1 else "are"
        if not resolution_outcome.dry_run:
            verb = "was" if summary.resolved_count == 1 else "were"
        action = "ready to resolve" if resolution_outcome.dry_run else "auto-resolved"
        summary_lines.append("")
        summary_lines.append(f"{summary.resolved_count} prior Codex {noun} {verb} {action}.")

    summary_lines.append("")
    summary_lines.append(SUMMARY_TIP)
    return "\n".join(summary_lines)


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
        self.codex_client = codex_client or CodexClient(config)
        self.context_manager = ReviewContextWriter()
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

    def _latest_reviewed_head_sha(
        self,
        issue_comments: list[IssueCommentLikeProtocol],
    ) -> str | None:
        for comment in reversed(issue_comments):
            body = comment.body
            if not isinstance(body, str) or SUMMARY_MARKER not in body:
                continue
            reviewed_head_sha = parse_reviewed_head_sha(body)
            if reviewed_head_sha:
                return reviewed_head_sha
        return None

    def _resume_cache_was_restored(self) -> bool:
        cache_hit = os.environ.get("CODEX_REVIEW_CACHE_HIT")
        if cache_hit is None:
            return True
        return cache_hit.strip().lower() == "true"

    def _resolve_review_resume_state(
        self,
        issue_comments: list[IssueCommentLikeProtocol],
        *,
        head_sha: str,
    ) -> _ReviewResumeState | None:
        previous_reviewed_sha = os.environ.get("CODEX_REVIEW_PREVIOUS_HEAD_SHA")
        if previous_reviewed_sha is not None:
            previous_reviewed_sha = previous_reviewed_sha.strip() or None
        if previous_reviewed_sha is None:
            previous_reviewed_sha = self._latest_reviewed_head_sha(issue_comments)
        if previous_reviewed_sha is None:
            self._debug(1, "No prior reviewed HEAD SHA found; starting fresh review")
            return None
        if not self._resume_cache_was_restored():
            self._debug(
                1,
                f"Resume cache miss for prior reviewed SHA {previous_reviewed_sha}; starting fresh",
            )
            return None

        codex_home_value = os.environ.get("CODEX_HOME")
        if not isinstance(codex_home_value, str) or not codex_home_value.strip():
            self._debug(1, "CODEX_HOME is unset; starting fresh review")
            return None

        try:
            is_ancestor = git_is_ancestor(previous_reviewed_sha, head_sha)
        except subprocess.CalledProcessError as exc:
            self._debug(
                1,
                "Failed to validate review resume ancestry "
                f"{previous_reviewed_sha} -> {head_sha}: {exc}",
            )
            return None
        if not is_ancestor:
            self._debug(
                1,
                f"Prior reviewed SHA {previous_reviewed_sha} is not an ancestor of {head_sha}; starting fresh",
            )
            return None

        codex_home = Path(codex_home_value)
        resume_thread_id = load_latest_thread_id(codex_home)
        if resume_thread_id is None:
            self._debug(
                1,
                f"No cached Codex thread found in {codex_home}; starting fresh review",
            )
            return None

        revision_range = f"{previous_reviewed_sha}..{head_sha}"
        try:
            incremental_diff = git_diff_text(revision_range)
            commit_shas = tuple(git_commit_shas(revision_range))
        except subprocess.CalledProcessError as exc:
            self._debug(
                1,
                f"Failed to compute incremental review context for {revision_range}: {exc}",
            )
            return None

        diff_line_count = len(incremental_diff.splitlines())
        inline_diff = None
        if diff_line_count <= MAX_INLINE_INCREMENTAL_DIFF_LINES:
            inline_diff = incremental_diff.strip() or None
        self._debug(
            1,
            "Resuming review from "
            f"{previous_reviewed_sha} with thread {resume_thread_id}; "
            f"incremental diff lines={diff_line_count}, "
            f"{'embedding diff' if inline_diff is not None else 'using commit range only'}",
        )
        return _ReviewResumeState(
            previous_reviewed_sha=previous_reviewed_sha,
            resume_thread_id=resume_thread_id,
            inline_diff=inline_diff,
            commit_shas=commit_shas,
        )

    def _build_review_resume_block(
        self,
        resume_state: _ReviewResumeState | None,
        *,
        head_sha: str,
    ) -> str:
        if resume_state is None:
            return ""

        lines = [
            "<review_resume_context>",
            "This is a continuation of an existing review conversation.",
            "Only review changes introduced since the previously reviewed commit.",
            f"<previous_reviewed_head_sha>{resume_state.previous_reviewed_sha}</previous_reviewed_head_sha>",
            f"<current_head_sha>{head_sha}</current_head_sha>",
        ]
        if resume_state.inline_diff is not None:
            lines.extend(
                [
                    "<incremental_diff>",
                    resume_state.inline_diff,
                    "</incremental_diff>",
                ]
            )
        else:
            lines.extend(["<incremental_commits>"])
            lines.extend(resume_state.commit_shas)
            lines.extend(
                [
                    "</incremental_commits>",
                    "Inspect the incremental delta locally with "
                    f"`git diff {resume_state.previous_reviewed_sha}..{head_sha}` as needed.",
                ]
            )
        lines.append("</review_resume_context>")
        return "\n".join(lines)

    def _build_schema_prompt(self, existing_comments: list[PriorCodexReviewComment]) -> str:
        """Build the turn-2 prompt for structured output, with optional dedup context."""
        prompt_context = render_prior_codex_comments_for_prompt(existing_comments)
        lines: list[str] = []
        if prompt_context:
            lines.append(prompt_context)
            lines.append(
                "Produce the JSON review output now. "
                'Use "findings" only for new, non-redundant findings from this review run. '
                'Use "carried_forward" only for entries from prior_codex_review_comments '
                "that still describe live issues in the current patch. "
                "For each carried_forward entry, copy the exact current_code snippet into "
                '"current_evidence" verbatim. '
                'Use "resolved_comment_ids" only for entries from '
                "prior_codex_review_comments_candidate_resolutions that now look fixed. "
                "Do not include stale or fixed comments in carried_forward. "
                "Do not include a carried-forward entry for an issue already captured in findings. "
                "Do not include a resolved_comment_ids entry for an issue that is still present "
                "or already captured in findings."
            )
        else:
            lines.append(
                'Produce the JSON review output now. Return "carried_forward" as [] and '
                '"resolved_comment_ids" as [].'
            )
        return "\n".join(lines)

    def _build_rename_map(self, changed_files: list[ChangedFileProtocol]) -> dict[str, str]:
        rename_map: dict[str, str] = {}
        for changed_file in changed_files:
            if changed_file.status != "renamed":
                continue
            previous_filename = changed_file.previous_filename
            current_filename = changed_file.filename
            if previous_filename and current_filename:
                rename_map[previous_filename] = current_filename
        return rename_map

    def _require_head_sha(self, pr: PullRequestLikeProtocol) -> str:
        head_sha = pr.head.sha if pr.head else None
        if head_sha:
            return head_sha
        raise ReviewContractError(
            f"Missing PR head commit SHA for {self.config.repository}#{pr.number}"
        )

    def _debug_changed_files(self, changed_files: list[ChangedFileProtocol]) -> None:
        self._debug(1, f"Changed files: {len(changed_files)}")
        for changed_file in changed_files[:10]:
            patch_len = (
                len(changed_file.patch.splitlines()) if isinstance(changed_file.patch, str) else 0
            )
            self._debug(
                2,
                f" - {changed_file.filename} status={changed_file.status} patch_len={patch_len}",
            )

    def _capture_review_snapshots(
        self,
        pr: PullRequestLikeProtocol,
        *,
        repo_root: Path,
    ) -> _ReviewSnapshots:
        try:
            review_comments_snapshot = list(pr.get_review_comments())
        except Exception as exc:
            raise ReviewContractError(
                f"Failed to retrieve review comments for {self.config.repository}#{pr.number}: {exc}"
            ) from exc
        try:
            issue_comments_snapshot = list(pr.get_issue_comments())
        except Exception as exc:
            raise ReviewContractError(
                f"Failed to retrieve issue comments for {self.config.repository}#{pr.number}: {exc}"
            ) from exc
        prior_codex_comments: list[PriorCodexReviewComment] = []
        codex_author_logins = collect_codex_author_logins(issue_comments_snapshot)
        if codex_author_logins:
            try:
                review_threads_snapshot = self.github_client.get_review_threads(pr)
            except Exception as exc:
                raise ReviewContractError(
                    "Failed to retrieve review thread state for "
                    f"{self.config.repository}#{pr.number}: {exc}"
                ) from exc
            prior_codex_comments = collect_prior_codex_review_comments(
                review_threads_snapshot,
                codex_author_logins,
                repo_root,
            )
        return _ReviewSnapshots(
            review_comments=review_comments_snapshot,
            issue_comments=issue_comments_snapshot,
            prior_codex_comments=prior_codex_comments,
        )

    def _sanitize_review_result(
        self,
        result: ReviewRunResult,
        prior_codex_comments: list[PriorCodexReviewComment],
    ) -> ReviewRunResult:
        carried_forward = self._normalize_carried_forward(
            result.carried_forward,
            prior_codex_comments,
        )
        resolved_comment_ids = self._normalize_resolved_comment_ids(
            result.resolved_comment_ids,
            prior_codex_comments,
            {item.comment_id for item in carried_forward},
        )
        if (
            carried_forward == result.carried_forward
            and resolved_comment_ids == result.resolved_comment_ids
        ):
            return result
        return ReviewRunResult(
            overall_correctness=result.overall_correctness,
            overall_explanation=result.overall_explanation,
            overall_confidence_score=result.overall_confidence_score,
            findings=list(result.findings),
            carried_forward=carried_forward,
            resolved_comment_ids=resolved_comment_ids,
        )

    def _normalize_carried_forward(
        self,
        raw_carried_forward: list[CarriedForwardReviewComment],
        prior_codex_comments: list[PriorCodexReviewComment],
    ) -> list[CarriedForwardReviewComment]:
        valid_comments = {
            comment.id: comment
            for comment in prior_codex_comments
            if comment.is_currently_applicable
        }
        normalized_carried_forward: list[CarriedForwardReviewComment] = []
        seen_comment_ids: set[str] = set()
        dropped_count = 0
        for carried_forward in raw_carried_forward:
            comment_id = carried_forward.comment_id
            valid_comment = valid_comments.get(comment_id)
            if comment_id in seen_comment_ids or valid_comment is None:
                dropped_count += 1
                continue
            current_evidence = carried_forward.current_evidence.strip()
            if current_evidence != valid_comment.current_code.strip():
                dropped_count += 1
                continue
            normalized_carried_forward.append(
                CarriedForwardReviewComment(
                    comment_id=comment_id,
                    current_evidence=valid_comment.current_code,
                )
            )
            seen_comment_ids.add(comment_id)
        if dropped_count > 0:
            self._debug(
                1,
                f"Dropped {dropped_count} invalid carried_forward entries from structured output",
            )
        return normalized_carried_forward

    def _normalize_resolved_comment_ids(
        self,
        raw_resolved_comment_ids: list[str],
        prior_codex_comments: list[PriorCodexReviewComment],
        carried_forward_comment_ids: set[str],
    ) -> list[str]:
        valid_comment_ids = {
            comment.id for comment in prior_codex_comments if not comment.is_currently_applicable
        }
        normalized_resolved_comment_ids: list[str] = []
        seen_comment_ids: set[str] = set()
        dropped_count = 0
        for comment_id in raw_resolved_comment_ids:
            if (
                comment_id in seen_comment_ids
                or comment_id not in valid_comment_ids
                or comment_id in carried_forward_comment_ids
            ):
                dropped_count += 1
                continue
            normalized_resolved_comment_ids.append(comment_id)
            seen_comment_ids.add(comment_id)
        if dropped_count > 0:
            self._debug(
                1,
                f"Dropped {dropped_count} invalid resolved_comment_ids entries from structured output",
            )
        return normalized_resolved_comment_ids

    def _build_summary(
        self,
        review: ReviewRunResult,
        resolution_outcome: ThreadResolutionOutcome,
    ) -> ReviewSummary:
        current_findings_count = len(review.findings)
        carried_forward_count = len(review.carried_forward)
        active_findings_count = current_findings_count + carried_forward_count
        overall_correctness = review.overall_correctness.strip()
        if not overall_correctness:
            overall_correctness = (
                "patch is incorrect" if active_findings_count else "patch is correct"
            )
        elif active_findings_count > 0 and overall_correctness.casefold() == "patch is correct":
            overall_correctness = "patch is incorrect"
        return ReviewSummary(
            overall_correctness=overall_correctness,
            current_findings_count=current_findings_count,
            carried_forward_count=carried_forward_count,
            active_findings_count=active_findings_count,
            resolved_count=resolution_outcome.resolved_count,
            resolution_failure_count=resolution_outcome.failed_count,
        )

    def _parse_structured_review_output(self, output: str) -> ReviewRunResult:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as parse_err:
            preview = output.strip()
            if not preview:
                preview = "(empty response)"
            if len(preview) > 1200:
                preview = preview[:1200] + "\n\n... (truncated)"
            self._debug(1, f"Structured output was not valid JSON: {parse_err}")
            print("Model did not return valid JSON (truncated preview):")
            print(preview)
            raise CodexExecutionError(f"JSON parsing error: {parse_err}") from parse_err

        try:
            return ReviewRunResult.from_payload(payload)
        except ReviewContractError:
            raise
        except Exception as exc:
            raise ReviewContractError(f"Invalid structured review output: {exc}") from exc

    def _publish_summary(self, pr: PullRequestLikeProtocol, summary: str) -> None:
        if self.config.dry_run:
            self._debug(1, "DRY_RUN: would refresh summary issue comment")
            return

        delete_warnings = self._delete_prior_summary(pr)
        for warning in delete_warnings:
            print(warning, file=sys.stderr)
        pr.as_issue().create_comment(summary)

    def process_review(self, pr_number: int) -> ReviewWorkflowResult:
        """Process a code review for the given pull request."""
        self._debug(1, f"Processing review for {self.config.repository} PR #{pr_number}")

        pr = self.github_client.get_pr(pr_number)
        changed_files = list(pr.get_files())
        rename_map = self._build_rename_map(changed_files)
        head_sha = self._require_head_sha(pr)
        self._debug_changed_files(changed_files)

        repo_root = self.config.resolved_repo_root
        context_dir_name = self.config.resolved_context_dir_name
        artifacts = ReviewArtifacts(repo_root=repo_root, context_dir_name=context_dir_name)
        snapshots = self._capture_review_snapshots(pr, repo_root=repo_root)
        self.context_manager.write_context_artifacts(
            pr,
            artifacts,
            issue_comments=snapshots.issue_comments,
            review_comments=snapshots.review_comments,
        )

        guidelines = load_guidelines(self.config)
        raw_prompt = compose_prompt(self.config, changed_files, pr, artifacts)
        base_instructions = self._build_review_base_instructions(guidelines)
        resume_state = self._resolve_review_resume_state(
            snapshots.issue_comments,
            head_sha=head_sha,
        )
        resume_block = self._build_review_resume_block(
            resume_state,
            head_sha=head_sha,
        )
        prompt_sections = [base_instructions]
        if resume_block:
            prompt_sections.append(resume_block)
        prompt_sections.append(raw_prompt)
        prompt = "\n\n".join(section for section in prompt_sections if section)

        self._debug(2, f"Prompt length: {len(prompt)} chars")

        schema_prompt = self._build_schema_prompt(snapshots.prior_codex_comments)

        print("Running Codex to generate review findings...", flush=True)

        output = self.codex_client.execute_structured(
            prompt,
            sandbox_mode="danger-full-access",
            output_schema=REVIEW_OUTPUT_SCHEMA,
            schema_prompt=schema_prompt,
            resume_thread_id=resume_state.resume_thread_id if resume_state is not None else None,
        )

        parsed_result = self._sanitize_review_result(
            self._parse_structured_review_output(output),
            snapshots.prior_codex_comments,
        )

        posting_outcome = self._post_results(
            parsed_result,
            changed_files,
            pr,
            head_sha,
            rename_map,
        )
        resolution_outcome = self._resolve_prior_codex_threads(
            parsed_result,
            snapshots.prior_codex_comments,
            pr,
        )
        summary = self._build_summary(parsed_result, resolution_outcome)

        summary_text = _build_review_summary(
            parsed_result,
            summary,
            posting_outcome,
            resolution_outcome,
            reviewed_head_sha=head_sha,
        )
        self._publish_summary(pr, summary_text)

        return ReviewWorkflowResult(
            review=parsed_result,
            posting_outcome=posting_outcome,
            resolution_outcome=resolution_outcome,
            summary=summary,
        )

    def _resolve_prior_codex_threads(
        self,
        result: ReviewRunResult,
        prior_codex_comments: list[PriorCodexReviewComment],
        pr: PullRequestLikeProtocol,
    ) -> ThreadResolutionOutcome:
        if not result.resolved_comment_ids:
            return ThreadResolutionOutcome.empty(dry_run=self.config.dry_run)

        comment_by_id = {
            comment.id: comment
            for comment in prior_codex_comments
            if not comment.is_currently_applicable
        }
        resolved_count = 0
        failed_count = 0
        seen_thread_ids: set[str] = set()
        for comment_id in result.resolved_comment_ids:
            comment = comment_by_id.get(comment_id)
            if comment is None or comment.thread_id in seen_thread_ids:
                continue
            seen_thread_ids.add(comment.thread_id)
            if self.config.dry_run:
                self._debug(
                    1,
                    "DRY_RUN: would resolve prior Codex review thread "
                    f"{comment.thread_id} for comment {comment_id}",
                )
                resolved_count += 1
                continue
            try:
                self.github_client.resolve_review_thread(pr, comment.thread_id)
                resolved_count += 1
                self._debug(
                    1,
                    f"Resolved prior Codex review thread {comment.thread_id} for comment {comment_id}",
                )
            except Exception as exc:
                failed_count += 1
                self._debug(
                    1,
                    "Failed to resolve prior Codex review thread "
                    f"{comment.thread_id} for comment {comment_id}: {exc}",
                )
        return ThreadResolutionOutcome(
            resolved_count=resolved_count,
            failed_count=failed_count,
            dry_run=self.config.dry_run,
        )

    def _post_results(
        self,
        result: ReviewRunResult,
        changed_files: list[ChangedFileProtocol],
        pr: PullRequestLikeProtocol,
        head_sha: str,
        rename_map: dict[str, str],
    ) -> ReviewPostingOutcome:
        """Post review results to GitHub."""
        findings = list(result.findings)
        total_findings = len(findings)

        file_maps = build_anchor_maps(changed_files)
        repo_root = self.config.resolved_repo_root
        artifacts = ReviewArtifacts(
            repo_root=repo_root,
            context_dir_name=self.config.resolved_context_dir_name,
        )
        persist_anchor_maps(file_maps, artifacts)

        build_result = build_inline_comment_payloads(
            findings,
            file_maps,
            rename_map,
            repo_root,
            dry_run=self.config.dry_run,
            debug=self._debug,
        )
        if build_result.dropped_count > 0:
            print(
                "Posting dropped "
                f"{build_result.dropped_count}/{total_findings} findings before GitHub comment creation "
                f"({build_result.describe_drops()})"
            )
        post_result = post_inline_comments(
            self.github_client,
            pr,
            head_sha,
            build_result.payloads,
            dry_run=self.config.dry_run,
            debug=self._debug,
        )
        return ReviewPostingOutcome(
            total_findings=total_findings,
            prefiltered_count=0,
            build_result=build_result,
            post_result=post_result,
        )

    def _delete_prior_summary(self, pr: PullRequestLikeProtocol) -> list[str]:
        """Delete prior Codex summary issue comments."""
        warnings: list[str] = []
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
                warning = f"Failed to delete prior summary issue comment id={comment.id}: {exc}"
                warnings.append(warning)
                self._debug(
                    1,
                    warning,
                )
        return warnings
