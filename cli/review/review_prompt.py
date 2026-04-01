from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ..core.config import ReviewConfig, make_debug
from ..core.exceptions import PromptError
from ..core.github_types import BaseRefLikeProtocol, ChangedFileProtocol, HeadRefLikeProtocol
from .artifacts import ReviewArtifacts


class ReviewPromptPullRequestProtocol(Protocol):
    title: str
    head: HeadRefLikeProtocol | None
    base: BaseRefLikeProtocol | None


def load_guidelines(config: ReviewConfig) -> str:
    if config.mode != "review":
        return ""

    debug = make_debug(config)
    builtin_path = Path(__file__).resolve().parents[2] / "prompts" / "review.md"

    try:
        debug(1, f"Using built-in prompt: {builtin_path}")
        return builtin_path.read_text(encoding="utf-8")
    except Exception as exc:
        debug(1, f"Failed reading built-in prompt file {builtin_path}: {exc}")
        raise PromptError(f"Failed to read built-in guidelines file {builtin_path}: {exc}") from exc


def compose_prompt(
    config: ReviewConfig,
    changed_files: Sequence[ChangedFileProtocol],
    pr: ReviewPromptPullRequestProtocol,
    artifacts: ReviewArtifacts,
) -> str:
    head_fields = _build_ref_fields(pr.head)
    base_fields = _build_ref_fields(pr.base)
    context = _build_context_block(
        pr_title=pr.title, artifacts=artifacts, head=head_fields, base=base_fields
    )

    line_rules = (
        "<line_rules>\n"
        "- Always use HEAD (right side) line numbers for code_location.\n"
        "- Prefer the exact added line(s) that contain the problematic code/text, not surrounding blanks.\n"
        "- Never select a trailing blank line. If your intended target is a blank line, shift to the nearest non-blank line (prefer earlier).\n"
        "- Keep ranges minimal; for single-line issues, set start=end to the single non-blank line.\n"
        "- Your line_range must overlap a visible + or context line in the diff hunk.\n"
        "- When you quote text in the body, align code_location.start to the line that contains that quote.\n"
        "</line_rules>\n"
    )

    changed_summary = _build_changed_summary(changed_files)

    pr_metadata_rel = artifacts.relative_to_repo_root(artifacts.pr_metadata_path)
    review_comments_rel = artifacts.relative_to_repo_root(artifacts.review_comments_path)

    context_artifacts = (
        "<context_artifacts>\n"
        f"<pr_metadata>{pr_metadata_rel}</pr_metadata>\n"
        f"<review_comments>{review_comments_rel}</review_comments>\n"
        "</context_artifacts>\n"
    )

    base_ref = base_fields.ref or base_fields.label.split(":", 1)[-1]
    review_instructions = (
        "<git_review_instructions>\n"
        "To view the exact changes to be merged, run:\n"
        f"  git diff origin/{base_ref}...HEAD\n"
        "(Triple dots compute the merge base automatically.)\n"
        "Consult {review_comments_rel} for context and provide prioritized, actionable findings.\n"
        "</git_review_instructions>\n"
    ).format(review_comments_rel=review_comments_rel)

    return (
        f"{context}"
        f"{context_artifacts}"
        f"{changed_summary}"
        f"{review_instructions}"
        f"{line_rules}"
        f"{render_additional_review_instructions(config)}"
        "<response_format>Respond now with the JSON schema output only.</response_format>"
    )


def render_additional_review_instructions(config: ReviewConfig) -> str:
    extra = config.additional_prompt
    if not extra:
        return ""
    return "<additional_instructions>\n" + extra + "\n</additional_instructions>\n"


def _build_changed_summary(changed_files: Sequence[ChangedFileProtocol]) -> str:
    changed_list = [f"- {file.filename} ({file.status})" for file in changed_files if file.filename]
    if not changed_list:
        return "<changed_files>(none)</changed_files>\n"
    return "<changed_files>\n" + "\n".join(changed_list) + "\n</changed_files>\n"


class _RefFields:
    def __init__(self, *, label: str, sha: str, ref: str) -> None:
        self.label = label
        self.sha = sha
        self.ref = ref


def _build_ref_fields(ref_obj: HeadRefLikeProtocol | BaseRefLikeProtocol | None) -> _RefFields:
    if ref_obj is None:
        return _RefFields(label="", sha="", ref="")
    return _RefFields(
        label=ref_obj.label if isinstance(ref_obj.label, str) else "",
        sha=ref_obj.sha if isinstance(ref_obj.sha, str) else "",
        ref=ref_obj.ref if isinstance(ref_obj.ref, str) else "",
    )


def _build_context_block(
    *,
    pr_title: object,
    artifacts: ReviewArtifacts,
    head: _RefFields,
    base: _RefFields,
) -> str:
    title_text = pr_title if isinstance(pr_title, str) else ""
    return (
        "<pull_request>\n"
        f"<title>{title_text}</title>\n"
        f'<head label="{head.label}" sha="{head.sha}" ref="{head.ref}"/>\n'
        f'<base label="{base.label}" sha="{base.sha}" ref="{base.ref}"/>\n'
        "</pull_request>\n"
        "<paths>\n"
        f"<repo_root>{artifacts.repo_root}</repo_root>\n"
        f"<context_dir>{artifacts.base_dir}</context_dir>\n"
        "</paths>\n"
    )
