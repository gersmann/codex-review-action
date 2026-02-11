from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..anchor_engine import resolve_range
from ..github_client import post_pr_resource
from ..models import FindingLocation, InlineCommentPayload
from ..patch_parser import to_relative_path


def persist_anchor_maps(
    file_maps: Mapping[str, Any],
    repo_root: Path,
    context_dir_name: str,
) -> None:
    base_dir = (repo_root / context_dir_name).resolve()
    payload = {
        path: {
            "valid_head_lines": sorted(list(parsed.valid_head_lines)),
            "added_head_lines": sorted(list(parsed.added_head_lines)),
            "positions_by_head_line": {
                str(line): position for line, position in parsed.positions_by_head_line.items()
            },
            "hunks": parsed.hunks,
        }
        for path, parsed in file_maps.items()
    }
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "anchor_maps.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_inline_comment_payloads(
    findings: Sequence[dict[str, Any]],
    file_maps: Mapping[str, Any],
    rename_map: Mapping[str, str],
    repo_root: Path,
    *,
    dry_run: bool,
    debug: Callable[[int, str], None],
) -> list[InlineCommentPayload]:
    payloads: list[InlineCommentPayload] = []

    for finding in findings:
        title = str(finding.get("title", "Issue")).strip()
        body = str(finding.get("body", "")).strip()
        location = FindingLocation.from_finding(finding)

        if not location.absolute_file_path or location.start_line <= 0:
            continue

        rel_path = to_relative_path(location.absolute_file_path, repo_root)
        rel_path = rename_map.get(rel_path, rel_path)
        file_map = file_maps.get(rel_path)
        if not file_map:
            continue

        has_suggestion = "```suggestion" in body
        anchor = resolve_range(
            rel_path,
            location.start_line,
            location.end_line,
            has_suggestion,
            file_map,
        )
        if not anchor:
            if dry_run:
                debug(
                    1,
                    (
                        "DRY_RUN: would skip (no anchor) for "
                        f"{rel_path}:{location.start_line}-{location.end_line}"
                    ),
                )
            continue

        final_body = body
        if has_suggestion and not (
            anchor.get("allow_suggestion") and anchor.get("kind") == "range"
        ):
            final_body = body.replace("```suggestion", "```diff")

        comment_body = f"{title}\n\n{final_body}" if final_body else title

        if anchor["kind"] == "range":
            payloads.append(
                InlineCommentPayload(
                    body=comment_body,
                    path=rel_path,
                    side="RIGHT",
                    line=int(anchor["end_line"]),
                    start_line=int(anchor["start_line"]),
                    start_side="RIGHT",
                )
            )
        else:
            payloads.append(
                InlineCommentPayload(
                    body=comment_body,
                    path=rel_path,
                    side="RIGHT",
                    line=int(anchor["line"]),
                )
            )

    return payloads


def post_inline_comments(
    pr: Any,
    head_sha: str,
    payloads: Sequence[InlineCommentPayload],
    *,
    dry_run: bool,
    debug: Callable[[int, str], None],
) -> None:
    if not payloads:
        if dry_run:
            debug(1, "DRY_RUN: no inline findings to post")
        return

    for payload in payloads:
        request_payload = payload.to_request_payload(head_sha)

        if dry_run:
            debug(
                1,
                (
                    "DRY_RUN: would POST /comments for "
                    f"{request_payload.get('path')}:{request_payload.get('line')}"
                ),
            )
            continue

        post_pr_resource(pr, "comments", request_payload)
