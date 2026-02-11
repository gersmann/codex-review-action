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
from ..models import REVIEW_OUTPUT_SCHEMA, ReviewRunResult
from ..review.dedupe import (
    SUMMARY_MARKER,
    collect_existing_comment_texts,
    collect_existing_review_comments,
    has_prior_codex_review,
    prefilter_duplicates_by_location,
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
    summary_lines = [
        SUMMARY_MARKER,
        f"- Overall: {result.overall_correctness.strip() or 'patch is correct'}",
        f"- Findings (total): {len(result.findings)}",
    ]

    overall_explanation = result.overall_explanation.strip()
    if overall_explanation:
        summary_lines.append("")
        summary_lines.append(overall_explanation)

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

    def _build_schema_prompt(self, existing_comments: list[Any]) -> str:
        """Build the turn-2 prompt for structured output, with optional dedup context."""
        existing_texts = collect_existing_comment_texts(existing_comments)
        if not existing_texts:
            return "Produce the JSON review output now."

        lines = ["<existing_review_comments>"]
        for text in existing_texts[:200]:
            lines.append(text)
        lines.append("</existing_review_comments>")
        lines.append(
            "Produce the JSON review output now. "
            "Exclude any findings that are semantically redundant with the existing review comments above."
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

        self._debug(2, f"Prompt length: {len(prompt)} chars")

        # Fetch existing comments before execute() so we can feed them
        # into the structured-output turn for inline deduplication.
        review_comments_snapshot = list(pr.get_review_comments())
        issue_comments_snapshot = list(pr.get_issue_comments())
        reviews_snapshot = list(pr.get_reviews())
        had_prior_codex_review = has_prior_codex_review(reviews_snapshot, issue_comments_snapshot)

        schema_prompt = self._build_schema_prompt(
            review_comments_snapshot if had_prior_codex_review else [],
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

        summary = _build_review_summary(parsed_result)
        if not self.config.dry_run:
            self._delete_prior_summary(pr)
            pr.as_issue().create_comment(summary)
        else:
            self._debug(1, "DRY_RUN: would refresh summary issue comment")

        self._post_results(
            parsed_result,
            changed_files,
            pr,
            head_sha,
            rename_map,
            prior_codex_review=had_prior_codex_review,
            review_comments_snapshot=review_comments_snapshot,
        )
        return parsed_result.as_dict()

    def _post_results(
        self,
        result: ReviewRunResult | dict[str, Any],
        changed_files: list[File],
        pr: PullRequest,
        head_sha: str,
        rename_map: dict[str, str],
        *,
        prior_codex_review: bool | None = None,
        review_comments_snapshot: list[Any] | None = None,
    ) -> None:
        """Post review results to GitHub."""
        normalized = (
            result if isinstance(result, ReviewRunResult) else ReviewRunResult.from_payload(result)
        )
        findings: list[dict[str, Any]] = list(normalized.findings)

        review_comments = review_comments_snapshot
        should_dedupe = prior_codex_review
        if should_dedupe is None:
            issue_comments = list(pr.get_issue_comments())
            reviews = list(pr.get_reviews())
            should_dedupe = has_prior_codex_review(reviews, issue_comments)
        if review_comments is None:
            review_comments = list(pr.get_review_comments())

        # Location prefilter: cheap safety net after turn-2 semantic dedup.
        if should_dedupe:
            existing_struct = collect_existing_review_comments(review_comments)
            before_prefilter = len(findings)
            findings = prefilter_duplicates_by_location(
                findings,
                existing_struct,
                rename_map,
                self.config.repo_root or Path(".").resolve(),
            )
            dropped_prefilter = before_prefilter - len(findings)
            if dropped_prefilter > 0:
                print(
                    "Prefilter dropped "
                    f"{dropped_prefilter}/{before_prefilter} findings due to existing comments"
                )

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
