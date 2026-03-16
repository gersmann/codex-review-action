#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.config import ReviewConfig
from .core.exceptions import CodexReviewError, ConfigurationError
from .core.models import CommentContext
from .workflows.edit_workflow import EditWorkflow
from .workflows.review_workflow import ReviewWorkflow

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CommentCommand:
    command: str
    pr_number: int
    comment_ctx: CommentContext


def create_parser() -> argparse.ArgumentParser:
    """Create the command line argument parser."""
    parser = argparse.ArgumentParser(
        description="Autonomous code review using Codex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Review a specific PR (requires GITHUB_TOKEN)
  python -m cli.main --repo owner/repo --pr 123

  # Dry run mode
  python -m cli.main --repo owner/repo --pr 123 --dry-run

  # Use different model
  python -m cli.main --repo owner/repo --pr 123 --model gpt-4o --provider openai

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
        "--mode",
        dest="mode",
        choices=["review", "act"],
        default="review",
        help="Operation mode: 'review' (code review) or 'act' (autonomous editing) (default: review)",
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
        default="gpt-5.4",
        help="Model name (default: gpt-5.4)",
    )
    parser.add_argument(
        "--reasoning-effort",
        dest="reasoning_effort",
        choices=["minimal", "low", "medium", "high"],
        default="medium",
        help="Reasoning effort level (default: medium)",
    )
    parser.add_argument(
        "--web-search-mode",
        dest="web_search_mode",
        choices=["disabled", "cached", "live"],
        default="live",
        help="Web search mode (default: live)",
    )

    parser.add_argument(
        "--act-instructions",
        dest="act_instructions",
        help="Additional instructions for act mode (autonomous editing)",
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


def extract_edit_command(text: str) -> str | None:
    """Extract a /codex edit command from a comment body.

    Accepted forms:
      - "/codex <instructions>"
      - "/codex: <instructions>"
    Returns the instruction text to pass to the coding agent, or None.
    """
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    prefix = "/codex"
    if not low.startswith(prefix):
        return None

    if len(t) > len(prefix):
        next_char = t[len(prefix)]
        if next_char != ":" and not next_char.isspace():
            return None

    rest = t[len(prefix) :].lstrip().lstrip(":").strip()
    return rest or None


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
    command = extract_edit_command(body)
    if not command:
        return 0
    pending_command = _prepare_comment_command(config, comment, body, command)
    if pending_command is None:
        return 0
    return EditWorkflow(config).process_edit_command(
        pending_command.command,
        pending_command.pr_number,
        pending_command.comment_ctx,
    )


def _extract_event_comment(actions_event: dict[str, Any] | None) -> dict[str, Any] | None:
    if actions_event is None:
        return None
    comment = actions_event.get("comment")
    return comment if isinstance(comment, dict) else None


def _prepare_comment_command(
    config: ReviewConfig,
    comment: dict[str, Any],
    body: str,
    command: str,
) -> _CommentCommand | None:
    if config.mode != "act":
        print(
            "Ignoring /codex command because mode is "
            f"{config.mode!r}; set CODEX_MODE=act to enable comment-triggered edits."
        )
        return None

    if not _is_commenter_allowed(config, comment):
        return None

    pr_number = config.pr_number
    if pr_number is None:
        raise ConfigurationError("This workflow must be triggered by a PR-related event")

    comment_ctx = _build_comment_context(comment, body)
    return _CommentCommand(command=command, pr_number=pr_number, comment_ctx=comment_ctx)


def _is_commenter_allowed(config: ReviewConfig, comment: dict[str, Any]) -> bool:
    author_association = str(comment.get("author_association") or "")
    if config.is_commenter_allowed(author_association):
        return True
    print(
        "Ignoring /codex command from unauthorized commenter association "
        f"{author_association or '<missing>'}. Allowed: "
        f"{', '.join(config.allowed_commenter_associations) or '<none>'}."
    )
    return False


def _build_comment_context(comment: dict[str, Any], body: str) -> CommentContext:
    comment_ctx = CommentContext.from_mapping(
        {
            "id": comment.get("id"),
            "event_name": os.environ.get("GITHUB_EVENT_NAME", ""),
            "author": str((comment.get("user") or {}).get("login") or ""),
            "body": body,
        }
    )
    if comment_ctx is None:
        raise ConfigurationError("Invalid comment event payload: missing id or event name")
    return comment_ctx


def _run_mode_workflow(config: ReviewConfig) -> int:
    config.validate()
    if config.mode == "act":
        if not config.pr_number:
            raise ConfigurationError("--pr is required in act mode")
        if not config.act_instructions.strip():
            raise ConfigurationError("--act-instructions is required in act mode")

        edit_workflow = EditWorkflow(config)
        return edit_workflow.process_edit_command(
            config.act_instructions, config.pr_number, comment_ctx=None
        )

    if config.pr_number is None:
        raise ConfigurationError("--pr is required in review mode")
    workflow = ReviewWorkflow(config)
    result = workflow.process_review(config.pr_number)

    summary = result.summary
    if summary.carried_forward_count > 0:
        print(
            "\nReview completed: "
            f"{summary.overall_correctness}, "
            f"{summary.current_findings_count} new findings, "
            f"{summary.carried_forward_count} prior findings still relevant "
            f"({summary.active_findings_count} active total)"
        )
    else:
        print(
            "\nReview completed: "
            f"{summary.overall_correctness}, {summary.current_findings_count} findings"
        )
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

        return _run_mode_workflow(config)

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
