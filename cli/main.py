#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from .config import ReviewConfig
    from .exceptions import CodexReviewError, ConfigurationError
    from .workflows.edit_workflow import EditWorkflow
    from .workflows.review_workflow import ReviewWorkflow
except ImportError:
    # Direct execution: add repo root to path and import as package
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from cli.config import ReviewConfig
    from cli.exceptions import CodexReviewError, ConfigurationError
    from cli.workflows.edit_workflow import EditWorkflow
    from cli.workflows.review_workflow import ReviewWorkflow


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

    # GitHub configuration
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

    # Mode configuration
    parser.add_argument(
        "--mode",
        dest="mode",
        choices=["review", "act"],
        default="review",
        help="Operation mode: 'review' (code review) or 'act' (autonomous editing) (default: review)",
    )

    # Model configuration
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
        default="gpt-5",
        help="Model name (default: gpt-5)",
    )
    parser.add_argument(
        "--reasoning-effort",
        dest="reasoning_effort",
        choices=["minimal", "low", "medium", "high"],
        default="medium",
        help="Reasoning effort level (default: medium)",
    )
    # Fast model for deduplication
    parser.add_argument(
        "--fast-model",
        dest="fast_model_name",
        default="gpt-5-mini",
        help="Fast model for deduplication on repeated runs (default: gpt-5-mini)",
    )
    parser.add_argument(
        "--fast-reasoning-effort",
        dest="fast_reasoning_effort",
        choices=["minimal", "low", "medium", "high"],
        default="low",
        help="Reasoning effort for fast model (default: low)",
    )

    # Act mode configuration
    parser.add_argument(
        "--act-instructions",
        dest="act_instructions",
        help="Additional instructions for act mode (autonomous editing)",
    )

    # Output configuration
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

    # Repository configuration
    parser.add_argument(
        "--repo-root",
        dest="repo_root",
        type=Path,
        help="Repository root path (default: current directory)",
    )

    # GitHub Actions mode (hidden option for backward compatibility)
    parser.add_argument(
        "--github-actions",
        action="store_true",
        help=argparse.SUPPRESS,  # Hidden option
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
    if low.startswith(prefix):
        rest = t[len(prefix) :].lstrip().lstrip(":").strip()
        return rest or t
    return None


def main() -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    try:
        # Handle GitHub Actions mode
        if args.github_actions or (not args.repository and os.environ.get("GITHUB_ACTIONS")):
            # GitHub Actions mode - support PR + comment-based events
            event = load_github_event()
            pr_number = ReviewConfig.extract_pr_number_from_event(event)
            if not pr_number:
                raise ConfigurationError("This workflow must be triggered by a PR-related event")

            config = ReviewConfig.from_environment()
            config.pr_number = pr_number

            # Comment commands: invoke edit mode if a comment contains a /codex command
            if isinstance(event.get("comment"), dict):
                body = str(event["comment"].get("body") or "")
                cmd = extract_edit_command(body)
                if cmd:
                    comment_ctx = {
                        "id": int(event["comment"].get("id") or 0),
                        "event_name": os.environ.get("GITHUB_EVENT_NAME", ""),
                        "author": (event["comment"].get("user") or {}).get("login"),
                        "body": body,
                    }
                    edit_workflow = EditWorkflow(config)
                    return edit_workflow.process_edit_command(cmd, pr_number, comment_ctx)
                return 0
        else:
            # CLI mode - create config from args
            config_kwargs = {
                k: v
                for k, v in vars(args).items()
                if v is not None and k not in ("github_actions",)
            }

            config = ReviewConfig.from_args(**config_kwargs)

        # Validate configuration
        config.validate()

        # Branch by mode
        if config.mode == "act":
            if not config.pr_number:
                raise ConfigurationError("--pr is required in act mode")
            if not (config.act_instructions or "").strip():
                raise ConfigurationError("--act-instructions is required in act mode")

            edit_workflow = EditWorkflow(config)
            return edit_workflow.process_edit_command(
                config.act_instructions, config.pr_number, comment_ctx=None
            )
        else:
            # Review mode (default)
            workflow = ReviewWorkflow(config)
            result = workflow.process_review()

            # Print summary
            findings_count = len(result.get("findings", []))
            overall = result.get("overall_correctness", "unknown")
            print(f"\nReview completed: {overall}, {findings_count} findings")

            return 0

    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except CodexReviewError as e:
        print(f"Review error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        if args.debug_level >= 2:
            import traceback

            traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
