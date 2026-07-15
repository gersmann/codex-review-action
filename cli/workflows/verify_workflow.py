from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..clients.codex_client import CodexClient
from ..clients.github_client import GitHubClient, GitHubClientProtocol, is_ack_body
from ..core.config import ReviewConfig, make_debug
from ..core.exceptions import CodexExecutionError, ConfigurationError, ReviewContractError
from ..core.github_types import PullRequestLikeProtocol
from ..core.models import VERIFY_OUTPUT_SCHEMA, ReviewCommentSnapshot, VerifyRunResult
from ..core.review_usage import ReviewUsage
from ..review.review_prompt import (
    load_verify_guidelines,
    render_additional_review_instructions,
)
from .review_workflow import build_usage_summary_lines

_VERDICT_HEADLINES = {
    "correct": "the claim is correct",
    "incorrect": "the claim is incorrect",
    "uncertain": "the claim could not be confirmed from this repository",
}

_COMMAND_TOKENS = {"/review", "/verify"}

_VERDICT_REPLY_HEADER = "### Codex Verification"


def _is_thread_noise(body: str) -> bool:
    """True for bodies that are commands, bot verdicts, or queue acks, not review content."""
    if not body:
        return True
    if body.startswith(_VERDICT_REPLY_HEADER):
        return True
    if is_ack_body(body):
        return True
    return body.split(maxsplit=1)[0].lower() in _COMMAND_TOKENS


@dataclass(frozen=True)
class VerifyTarget:
    """What a /verify invocation is adjudicating."""

    claim: str
    thread_root_id: int | None
    path: str = ""
    line: int | None = None

    def ensure_verifiable(self) -> None:
        """Raise unless the target names a thread or states a claim."""
        if self.thread_root_id is None and not self.claim.strip():
            raise ConfigurationError(
                "Nothing to verify: reply to a review comment with /verify, "
                "or state the claim after the command."
            )


@dataclass(frozen=True)
class VerifyWorkflowResult:
    verify: VerifyRunResult
    usage: ReviewUsage | None = None


class VerifyWorkflow:
    """Workflow for adjudicating reviewer claims on demand."""

    def __init__(
        self,
        config: ReviewConfig,
        *,
        github_client: GitHubClientProtocol | None = None,
        codex_client: CodexClient | None = None,
    ) -> None:
        self.config = config
        self.codex_client = codex_client or CodexClient(config)
        self.github_client: GitHubClientProtocol = github_client or GitHubClient(config)
        self._debug = make_debug(config)

    def process_verify(self, pr_number: int, target: VerifyTarget) -> VerifyWorkflowResult:
        verify_started_at = time.monotonic()
        target.ensure_verifiable()

        pr = self.github_client.get_pr(pr_number)
        prompt = self._compose_prompt(pr, target)
        self._debug(2, f"Verify prompt length: {len(prompt)} chars")

        print("Running Codex to verify the claim...", flush=True)
        output = self.codex_client.execute_structured(
            prompt,
            sandbox_mode="danger-full-access",
            output_schema=VERIFY_OUTPUT_SCHEMA,
            schema_prompt="Produce the JSON verify output now.",
        )
        verify_result = self._parse_structured_verify_output(output)

        reply_body = self._build_reply(
            verify_result,
            elapsed_seconds=time.monotonic() - verify_started_at,
        )
        self._post_reply(pr, target, reply_body)
        return VerifyWorkflowResult(verify=verify_result, usage=self.codex_client.usage)

    def _compose_prompt(self, pr: PullRequestLikeProtocol, target: VerifyTarget) -> str:
        base_ref = pr.base.ref if pr.base is not None else "main"
        sections = [
            "You are an autonomous code review assistant.\n"
            "Follow the verification guidelines below verbatim.",
            load_verify_guidelines(self.config).strip(),
            (
                "<git_review_instructions>\n"
                "To view the exact changes to be merged, run:\n"
                f"  git diff origin/{base_ref}...HEAD\n"
                "(Triple dots compute the merge base automatically.)\n"
                "</git_review_instructions>"
            ),
        ]
        thread_block = self._render_thread(pr, target)
        claim = target.claim.strip()
        if not thread_block and not claim:
            raise ConfigurationError(
                "Nothing to verify: the review thread has no verifiable comments; "
                "state the claim after the command."
            )
        if thread_block:
            sections.append(thread_block)
        if claim:
            sections.append(f"<claim>\n{claim}\n</claim>")
        extra = render_additional_review_instructions(self.config).strip()
        if extra:
            sections.append(extra)
        sections.append(
            "<response_format>Respond now with the JSON schema output only.</response_format>"
        )
        return "\n\n".join(sections)

    def _render_thread(self, pr: PullRequestLikeProtocol, target: VerifyTarget) -> str:
        if target.thread_root_id is None:
            return ""
        root_id = target.thread_root_id
        thread = [
            snapshot
            for snapshot in (
                ReviewCommentSnapshot.from_review_comment(comment)
                for comment in pr.get_review_comments()
            )
            if snapshot.id == root_id or snapshot.in_reply_to_id == root_id
        ]
        rendered: list[str] = ["<review_thread>"]
        if target.path:
            location = f"{target.path}:{target.line}" if target.line else target.path
            rendered.append(f"<location>{location}</location>")
        body_count = 0
        for snapshot in thread:
            body = snapshot.body.strip()
            if _is_thread_noise(body):
                continue
            rendered.append(
                json.dumps({"author": snapshot.author, "body": body}, ensure_ascii=True)
            )
            body_count += 1
        if body_count == 0:
            return ""
        rendered.append("</review_thread>")
        return "\n".join(rendered)

    def _parse_structured_verify_output(self, output: str) -> VerifyRunResult:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as parse_err:
            raise CodexExecutionError(f"JSON parsing error: {parse_err}") from parse_err
        try:
            return VerifyRunResult.from_payload(payload)
        except ReviewContractError:
            raise
        except Exception as exc:
            raise ReviewContractError(f"Invalid structured verify output: {exc}") from exc

    def _build_reply(self, result: VerifyRunResult, *, elapsed_seconds: float) -> str:
        headline = _VERDICT_HEADLINES[result.verdict]
        lines = [
            _VERDICT_REPLY_HEADER,
            f"**Verdict:** {headline}.",
            "",
            result.explanation.strip(),
        ]
        if result.confidence_score is not None:
            lines.append("")
            lines.append(f"Confidence: {result.confidence_score:.2f}")
        lines.extend(
            build_usage_summary_lines(
                model_name=self.config.model_name,
                reasoning_effort=self.config.reasoning_effort,
                usage=self.codex_client.usage,
                elapsed_seconds=elapsed_seconds,
            )
        )
        return "\n".join(lines)

    def _post_reply(
        self,
        pr: PullRequestLikeProtocol,
        target: VerifyTarget,
        body: str,
    ) -> None:
        if self.config.dry_run:
            self._debug(1, "DRY_RUN: would post verification reply")
            return
        if target.thread_root_id is not None:
            self.github_client.reply_to_review_comment(pr, target.thread_root_id, body)
        else:
            self.github_client.post_issue_comment(pr, body)
