#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .clients.github_client import GitHubClient
from .core.config import ReviewConfig
from .core.exceptions import CodexReviewError, ConfigurationError
from .core.github_types import ReviewRequestContext
from .core.model_pricing import SUPPORTED_REVIEW_MODELS
from .core.models import AckComment
from .core.reasoning_effort import REASONING_EFFORT_VALUES, normalize_reasoning_effort
from .workflows.review_workflow import ReviewWorkflow
from .workflows.verify_workflow import VerifyTarget, VerifyWorkflow

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CodexCommandRequest:
    action: Literal["review", "verify"]
    model_name: str | None
    reasoning_effort: str | None
    claim: str = ""


def create_parser() -> argparse.ArgumentParser:
    """Create the command line argument parser."""
    parser = argparse.ArgumentParser(
        description="Code review using Codex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Review a specific PR (requires GITHUB_TOKEN)
  python -m cli.main --repo owner/repo --pr 123

  # Dry run mode
  python -m cli.main --repo owner/repo --pr 123 --dry-run

  # Use a different priced review model
  python -m cli.main --repo owner/repo --pr 123 --model gpt-5.6-terra

Environment Variables:
  GITHUB_TOKEN        GitHub API token (required)
  OPENAI_API_KEY      OpenAI API key (required for OpenAI provider)
  DEBUG_CODEREVIEW    Debug level (0-2, default: 0)
  DRY_RUN            Skip actual posting (1 for dry run)
        """,
    )

    parser.add_argument(
        "--repo",
        "--repository",
        dest="repository",
        help="Repository in format 'owner/repo'",
        required=False,
    )
    parser.add_argument(
        "--pr",
        "--pr-number",
        dest="pr_number",
        type=int,
        help="Pull request number to review",
    )
    parser.add_argument(
        "--token",
        dest="github_token",
        help="GitHub API token (or use GITHUB_TOKEN env var)",
    )

    parser.add_argument(
        "--provider",
        dest="model_provider",
        choices=["openai"],
        default="openai",
        help="Model provider (default: openai)",
    )
    parser.add_argument(
        "--model",
        dest="model_name",
        choices=SUPPORTED_REVIEW_MODELS,
        default="gpt-5.6-luna",
        help="Model name (default: gpt-5.6-luna)",
    )
    parser.add_argument(
        "--reasoning-effort",
        dest="reasoning_effort",
        type=normalize_reasoning_effort,
        choices=REASONING_EFFORT_VALUES,
        default=None,
        help=(
            f"Reasoning effort level: {' | '.join(REASONING_EFFORT_VALUES)} "
            "(default depends on the model: xhigh for gpt-5.6-luna, "
            "medium for gpt-5.6-terra and gpt-5.6-sol)"
        ),
    )
    parser.add_argument(
        "--web-search-mode",
        dest="web_search_mode",
        choices=["disabled", "cached", "live"],
        default="disabled",
        help="Web search mode (default: live)",
    )

    parser.add_argument(
        "--debug",
        dest="debug_level",
        type=int,
        choices=[0, 1, 2],
        default=0,
        help="Debug level (0-2, default: 0)",
    )
    parser.add_argument(
        "--no-stream",
        dest="stream_output",
        action="store_false",
        help="Disable streaming output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't post comments, just show what would be posted",
    )

    parser.add_argument(
        "--repo-root",
        dest="repo_root",
        type=Path,
        help="Repository root path (default: current directory)",
    )

    return parser


def load_github_event() -> dict[str, Any]:
    """Load GitHub event data from GitHub Actions environment."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise ConfigurationError("GITHUB_EVENT_PATH not set; are we in GitHub Actions?")

    try:
        with open(event_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ConfigurationError("Unexpected event payload type; expected object")
        return data
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigurationError(f"Failed to load GitHub event data: {e}") from e


def extract_command(text: str) -> str | None:
    """Extract a /review or /verify command from a comment body.

    Accepted forms:
      - "/review <options>" / "/verify <claim>"
      - "/review: <options>" / "/verify: <claim>"
    Returns the command text with the action as its first token, or None.
    """
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    for action in ("review", "verify"):
        prefix = f"/{action}"
        if not low.startswith(prefix):
            continue
        if len(t) > len(prefix):
            next_char = t[len(prefix)]
            if next_char != ":" and not next_char.isspace():
                continue
        rest = t[len(prefix) :].lstrip().lstrip(":").strip()
        return f"{action} {rest}" if rest else action
    return None


def _load_runtime_config(
    args: argparse.Namespace,
    event: dict[str, Any] | None,
) -> ReviewConfig:
    if event is not None:
        return ReviewConfig.from_github_event(event)

    config_kwargs = {k: v for k, v in vars(args).items() if v is not None}
    return ReviewConfig.from_args(**config_kwargs)


def _load_actions_event(repository: str | None) -> dict[str, Any] | None:
    if repository or not os.environ.get("GITHUB_ACTIONS"):
        return None
    return load_github_event()


def _handle_comment_event(
    config: ReviewConfig,
    actions_event: dict[str, Any] | None,
) -> int | None:
    comment = _extract_event_comment(actions_event)
    if comment is None:
        return None

    body = str(comment.get("body") or "")
    command = extract_command(body)
    if not command:
        return 0

    overrides = _parse_codex_command(command)

    if not _is_commenter_allowed(config, comment):
        return 0

    pr_number = config.pr_number
    if pr_number is None:
        raise ConfigurationError("This workflow must be triggered by a PR-related event")

    config_kwargs: dict[str, object] = {"pr_number": pr_number}
    if overrides.action == "review":
        config_kwargs["force_fresh_review"] = (
            overrides.model_name is not None or overrides.reasoning_effort is not None
        )
    if overrides.model_name is not None:
        config_kwargs["model_name"] = overrides.model_name
    if overrides.reasoning_effort is not None:
        config_kwargs["reasoning_effort"] = overrides.reasoning_effort

    run_config = ReviewConfig.from_args(**config_kwargs)
    request = _review_request_context(comment)

    target: VerifyTarget | None = None
    if overrides.action == "verify":
        target = _verify_target_from_comment(comment, overrides.claim, request.event_name)
        target.ensure_verifiable()

    github_client: GitHubClient | None = None
    ack: AckComment | None = None
    if not config.dry_run:
        github_client = GitHubClient(config)
        ack = github_client.acknowledge_review_request(
            pr_number,
            request,
            kind="verification" if target is not None else "review",
        )

    if target is not None:
        exit_code = _run_verify_workflow(run_config, target)
    else:
        exit_code = _run_review_workflow(run_config)

    if exit_code == 0 and github_client is not None and ack is not None:
        _delete_ack_comment_best_effort(github_client, pr_number, ack)
    return exit_code


def _verify_target_from_comment(
    comment: dict[str, Any],
    claim: str,
    event_name: str,
) -> VerifyTarget:
    if event_name != "pull_request_review_comment":
        return VerifyTarget(claim=claim, thread_root_id=None)
    comment_id = comment.get("id")
    in_reply_to = comment.get("in_reply_to_id")
    root_id = in_reply_to if isinstance(in_reply_to, int) else comment_id
    if not isinstance(root_id, int):
        raise ConfigurationError("Verify request comment is missing a numeric id")
    path = comment.get("path")
    line = comment.get("line")
    return VerifyTarget(
        claim=claim,
        thread_root_id=root_id,
        path=path if isinstance(path, str) else "",
        line=line if isinstance(line, int) else None,
    )


def _delete_ack_comment_best_effort(
    github_client: GitHubClient,
    pr_number: int,
    ack: AckComment,
) -> None:
    try:
        github_client.delete_ack_comment(pr_number, ack)
    except Exception as exc:
        print(
            f"Failed to delete acknowledgement comment id={ack.comment_id}: {exc}",
            file=sys.stderr,
        )


def _review_request_context(comment: dict[str, Any]) -> ReviewRequestContext:
    comment_id = comment.get("id")
    if not isinstance(comment_id, int):
        raise ConfigurationError("Review request comment is missing a numeric id")

    user = comment.get("user")
    if not isinstance(user, dict):
        raise ConfigurationError("Review request comment is missing its author")
    login = user.get("login")
    if not isinstance(login, str) or not login.strip():
        raise ConfigurationError("Review request comment is missing its author login")

    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "issue_comment":
        return ReviewRequestContext(comment_id, login, "issue_comment")
    if event_name == "pull_request_review_comment":
        return ReviewRequestContext(comment_id, login, "pull_request_review_comment")
    raise ConfigurationError(f"Unsupported review request event: {event_name or '<missing>'}")


def _extract_event_comment(actions_event: dict[str, Any] | None) -> dict[str, Any] | None:
    if actions_event is None:
        return None
    comment = actions_event.get("comment")
    return comment if isinstance(comment, dict) else None


_MODEL_SHORTHANDS = ("luna", "sol", "terra")
_MODEL_SHORTHAND_PREFIX = "gpt-5.6"


def _expand_model_shorthand(value: str) -> str:
    """Expand a bare model shorthand ("luna") to its full name ("gpt-5.6-luna")."""
    normalized = value.lower()
    if normalized in _MODEL_SHORTHANDS:
        return f"{_MODEL_SHORTHAND_PREFIX}-{normalized}"
    return value


def _record_option(values: dict[str, str], action: str, key: str, value: str) -> None:
    if key in values:
        raise ConfigurationError(f"Duplicate /{action} option: {key}")
    values[key] = value


def _parse_codex_command(command: str) -> _CodexCommandRequest:
    tokens = command.split()
    action_token = tokens[0].lower() if tokens else ""
    if action_token not in {"review", "verify"}:
        raise ConfigurationError(f"Unsupported Codex command: {command!r}")
    action = cast(Literal["review", "verify"], action_token)

    values: dict[str, str] = {}
    claim_words: list[str] = []
    for token in tokens[1:]:
        key, separator, value = token.partition(":")
        normalized_key = key.lower()
        if claim_words:
            claim_words.append(token)
        elif separator and normalized_key in {"model", "reasoning"}:
            if not value:
                raise ConfigurationError(f"Missing value for /{action} option: {normalized_key}")
            if normalized_key == "model":
                value = _expand_model_shorthand(value)
            _record_option(values, action, normalized_key, value)
        elif separator and normalized_key in _MODEL_SHORTHANDS:
            # "luna:high" sets both the model and its reasoning effort.
            if not value:
                raise ConfigurationError(f"Missing value for /{action} option: {normalized_key}")
            _record_option(values, action, "model", _expand_model_shorthand(normalized_key))
            _record_option(values, action, "reasoning", value)
        elif not separator and normalized_key in _MODEL_SHORTHANDS:
            _record_option(values, action, "model", _expand_model_shorthand(normalized_key))
        elif action == "verify":
            claim_words.append(token)
        else:
            raise ConfigurationError(f"Unsupported /review option: {token}")

    reasoning_effort = values.get("reasoning")
    if reasoning_effort is not None:
        try:
            reasoning_effort = normalize_reasoning_effort(reasoning_effort)
        except ValueError as error:
            raise ConfigurationError(str(error)) from error

    return _CodexCommandRequest(
        action=action,
        model_name=values.get("model"),
        reasoning_effort=reasoning_effort,
        claim=" ".join(claim_words),
    )


def _is_commenter_allowed(config: ReviewConfig, comment: dict[str, Any]) -> bool:
    author_association = str(comment.get("author_association") or "")
    if config.is_commenter_allowed(author_association):
        return True
    print(
        "Ignoring command from unauthorized commenter association "
        f"{author_association or '<missing>'}. Allowed: "
        f"{', '.join(config.allowed_commenter_associations) or '<none>'}."
    )
    return False


def _run_review_workflow(config: ReviewConfig) -> int:
    config.validate()
    if config.pr_number is None:
        raise ConfigurationError("--pr is required")
    workflow = ReviewWorkflow(config)
    result = workflow.process_review(config.pr_number)

    summary = result.summary
    if summary.carried_forward_count > 0:
        extra_parts: list[str] = []
        if summary.carried_forward_count > 0:
            extra_parts.append(f"{summary.carried_forward_count} prior findings still relevant")
        print(
            "\nReview completed: "
            f"{summary.overall_correctness}, "
            f"{summary.current_findings_count} new findings, "
            f"{', '.join(extra_parts)} "
            f"({summary.active_findings_count} active total)"
        )
    else:
        print(
            "\nReview completed: "
            f"{summary.overall_correctness}, {summary.current_findings_count} findings"
        )
    return 0


def _run_verify_workflow(config: ReviewConfig, target: VerifyTarget) -> int:
    config.validate()
    if config.pr_number is None:
        raise ConfigurationError("--pr is required")
    workflow = VerifyWorkflow(config)
    result = workflow.process_verify(config.pr_number, target)
    print(f"\nVerification completed: {result.verify.verdict}")
    return 0


def main() -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    try:
        actions_event = _load_actions_event(args.repository)
        config = _load_runtime_config(args, actions_event)

        comment_result = _handle_comment_event(config, actions_event)
        if comment_result is not None:
            return comment_result

        return _run_review_workflow(config)

    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except CodexReviewError as e:
        print(f"Review error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        if args.debug_level >= 2:
            LOGGER.exception("Unhandled exception in codex-review main")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
