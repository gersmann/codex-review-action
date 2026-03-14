from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from ..clients.codex_client import CodexClient
from ..clients.github_client import GitHubClient, GitHubClientProtocol
from ..core.config import ReviewConfig, make_debug
from ..core.exceptions import CodexExecutionError, ReviewContractError
from ..core.github_types import (
    ChangedFileProtocol,
    IssueCommentLikeProtocol,
    PullRequestLikeProtocol,
    ReviewCommentLikeProtocol,
    ReviewLikeProtocol,
)
from ..core.models import REVIEW_OUTPUT_SCHEMA, ReviewRunResult
from ..review.anchor_engine import build_anchor_maps
from ..review.artifacts import ReviewArtifacts
from ..review.context_manager import ReviewContextWriter
from ..review.dedupe import (
    SUMMARY_MARKER,
    collect_existing_comment_texts,
    collect_existing_review_comments,
    has_prior_codex_review,
    prefilter_duplicates_by_location,
)
from ..review.posting import (
    ReviewPostingOutcome,
    build_inline_comment_payloads,
    persist_anchor_maps,
    post_inline_comments,
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
    reviews: list[ReviewLikeProtocol]
    had_prior_codex_review: bool


@dataclass(frozen=True)
class ReviewWorkflowResult:
    review: ReviewRunResult
    posting_outcome: ReviewPostingOutcome


def _build_review_summary(result: ReviewRunResult, posting_outcome: ReviewPostingOutcome) -> str:
    summary_lines = [
        SUMMARY_MARKER,
        f"- Overall: {result.overall_correctness.strip() or 'patch is correct'}",
        f"- Findings (total): {len(result.findings)}",
    ]
    if posting_outcome.dropped_count > 0:
        summary_lines.append(
            f"- Findings not publishable: {posting_outcome.dropped_count} ({posting_outcome.describe_drops()})"
        )
    if posting_outcome.post_result.dry_run:
        summary_lines.append(f"- Inline comments ready: {posting_outcome.publishable_count}")
    elif posting_outcome.publishable_count > 0:
        summary_lines.append(f"- Inline comments posted: {posting_outcome.published_count}")

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

    def _build_schema_prompt(self, existing_comments: list[ReviewCommentLikeProtocol]) -> str:
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

    def _capture_review_snapshots(self, pr: PullRequestLikeProtocol) -> _ReviewSnapshots:
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
        try:
            reviews_snapshot = list(pr.get_reviews())
        except Exception as exc:
            raise ReviewContractError(
                f"Failed to retrieve reviews for {self.config.repository}#{pr.number}: {exc}"
            ) from exc
        return _ReviewSnapshots(
            review_comments=review_comments_snapshot,
            issue_comments=issue_comments_snapshot,
            reviews=reviews_snapshot,
            had_prior_codex_review=has_prior_codex_review(
                reviews_snapshot, issue_comments_snapshot
            ),
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
        snapshots = self._capture_review_snapshots(pr)
        self.context_manager.write_context_artifacts(
            pr,
            artifacts,
            issue_comments=snapshots.issue_comments,
            review_comments=snapshots.review_comments,
        )

        guidelines = load_guidelines(self.config)
        raw_prompt = compose_prompt(self.config, changed_files, pr, artifacts)
        base_instructions = self._build_review_base_instructions(guidelines)
        prompt = base_instructions + "\n\n" + raw_prompt

        self._debug(2, f"Prompt length: {len(prompt)} chars")

        schema_prompt = self._build_schema_prompt(
            snapshots.review_comments if snapshots.had_prior_codex_review else [],
        )

        print("Running Codex to generate review findings...", flush=True)

        output = self.codex_client.execute_structured(
            prompt,
            sandbox_mode="danger-full-access",
            output_schema=REVIEW_OUTPUT_SCHEMA,
            schema_prompt=schema_prompt,
        )

        parsed_result = self._parse_structured_review_output(output)

        posting_outcome = self._post_results(
            parsed_result,
            changed_files,
            pr,
            head_sha,
            rename_map,
            prior_codex_review=snapshots.had_prior_codex_review,
            review_comments_snapshot=snapshots.review_comments,
        )

        summary = _build_review_summary(parsed_result, posting_outcome)
        self._publish_summary(pr, summary)

        return ReviewWorkflowResult(
            review=parsed_result,
            posting_outcome=posting_outcome,
        )

    def _post_results(
        self,
        result: ReviewRunResult,
        changed_files: list[ChangedFileProtocol],
        pr: PullRequestLikeProtocol,
        head_sha: str,
        rename_map: dict[str, str],
        *,
        prior_codex_review: bool | None = None,
        review_comments_snapshot: list[ReviewCommentLikeProtocol] | None = None,
    ) -> ReviewPostingOutcome:
        """Post review results to GitHub."""
        findings = list(result.findings)
        total_findings = len(findings)

        review_comments = review_comments_snapshot
        should_dedupe = prior_codex_review
        dropped_prefilter = 0
        if should_dedupe is None:
            issue_comments: list[IssueCommentLikeProtocol] = list(pr.get_issue_comments())
            reviews: list[ReviewLikeProtocol] = list(pr.get_reviews())
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
                self.config.resolved_repo_root,
            )
            dropped_prefilter = before_prefilter - len(findings)
            if dropped_prefilter > 0:
                print(
                    "Prefilter dropped "
                    f"{dropped_prefilter}/{before_prefilter} findings due to existing comments"
                )

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
            prefiltered_count=dropped_prefilter,
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
