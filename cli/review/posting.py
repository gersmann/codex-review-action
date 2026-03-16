from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..clients.github_client import GitHubClientProtocol
from ..core.filesystem import write_text_atomic
from ..core.github_types import PullRequestLikeProtocol
from ..core.models import FindingLocation, InlineCommentPayload, ReviewFinding
from .anchor_engine import RangeAnchor, SingleAnchor, resolve_range
from .artifacts import ReviewArtifacts
from .patch_parser import ParsedPatch, to_relative_path


@dataclass(frozen=True)
class InlineCommentBuildResult:
    payloads: list[InlineCommentPayload]
    dropped_missing_location: int = 0
    dropped_missing_file_map: int = 0
    dropped_missing_anchor: int = 0

    @property
    def dropped_count(self) -> int:
        return (
            self.dropped_missing_location
            + self.dropped_missing_file_map
            + self.dropped_missing_anchor
        )

    def describe_drops(self) -> str:
        parts: list[str] = []
        if self.dropped_missing_location:
            parts.append(f"missing location={self.dropped_missing_location}")
        if self.dropped_missing_file_map:
            parts.append(f"missing file map={self.dropped_missing_file_map}")
        if self.dropped_missing_anchor:
            parts.append(f"missing anchor={self.dropped_missing_anchor}")
        return ", ".join(parts)


@dataclass(frozen=True)
class InlineCommentPostResult:
    attempted_count: int
    posted_count: int
    dry_run: bool = False


@dataclass(frozen=True)
class ReviewPostingOutcome:
    total_findings: int
    prefiltered_count: int
    build_result: InlineCommentBuildResult
    post_result: InlineCommentPostResult

    @classmethod
    def empty(cls, total_findings: int, *, dry_run: bool = False) -> ReviewPostingOutcome:
        return cls(
            total_findings=total_findings,
            prefiltered_count=0,
            build_result=InlineCommentBuildResult(payloads=[]),
            post_result=InlineCommentPostResult(attempted_count=0, posted_count=0, dry_run=dry_run),
        )

    @property
    def publishable_count(self) -> int:
        return len(self.build_result.payloads)

    @property
    def published_count(self) -> int:
        return self.post_result.posted_count

    @property
    def dropped_count(self) -> int:
        return self.prefiltered_count + self.build_result.dropped_count

    def describe_drops(self) -> str:
        parts: list[str] = []
        if self.prefiltered_count:
            parts.append(f"existing comments={self.prefiltered_count}")
        build_drops = self.build_result.describe_drops()
        if build_drops:
            parts.append(build_drops)
        return ", ".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_findings": self.total_findings,
            "prefiltered_count": self.prefiltered_count,
            "publishable_count": self.publishable_count,
            "published_count": self.published_count,
            "dropped_count": self.dropped_count,
            "dry_run": self.post_result.dry_run,
            "drop_reasons": self.describe_drops(),
        }


def persist_anchor_maps(
    file_maps: Mapping[str, ParsedPatch],
    artifacts: ReviewArtifacts,
) -> None:
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
    artifacts.base_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        artifacts.anchor_maps_path,
        json.dumps(payload, indent=2),
    )


def build_inline_comment_payloads(
    findings: Sequence[ReviewFinding],
    file_maps: Mapping[str, ParsedPatch],
    rename_map: Mapping[str, str],
    repo_root: Path,
    *,
    dry_run: bool,
    debug: Callable[[int, str], None],
) -> InlineCommentBuildResult:
    payloads: list[InlineCommentPayload] = []
    dropped_missing_file_map = 0
    dropped_missing_anchor = 0

    for finding in findings:
        title = finding.title.strip() or "Issue"
        body = finding.body.strip()
        location = FindingLocation.from_review_finding(finding)

        rel_path = to_relative_path(location.absolute_file_path, repo_root)
        rel_path = rename_map.get(rel_path, rel_path)
        file_map = file_maps.get(rel_path)
        if not file_map:
            dropped_missing_file_map += 1
            continue

        has_suggestion = "```suggestion" in body
        anchor = resolve_range(
            location.start_line,
            location.end_line,
            has_suggestion,
            file_map,
        )
        if not anchor:
            dropped_missing_anchor += 1
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
        if has_suggestion and not isinstance(anchor, RangeAnchor):
            final_body = body.replace("```suggestion", "```diff")

        comment_body = f"{title}\n\n{final_body}" if final_body else title

        if isinstance(anchor, RangeAnchor):
            payloads.append(
                InlineCommentPayload(
                    body=comment_body,
                    path=rel_path,
                    side="RIGHT",
                    line=anchor.end_line,
                    start_line=anchor.start_line,
                    start_side="RIGHT",
                )
            )
        if isinstance(anchor, SingleAnchor):
            payloads.append(
                InlineCommentPayload(
                    body=comment_body,
                    path=rel_path,
                    side="RIGHT",
                    line=anchor.line,
                )
            )

    return InlineCommentBuildResult(
        payloads=payloads,
        dropped_missing_location=0,
        dropped_missing_file_map=dropped_missing_file_map,
        dropped_missing_anchor=dropped_missing_anchor,
    )


def post_inline_comments(
    github_client: GitHubClientProtocol,
    pr: PullRequestLikeProtocol,
    head_sha: str,
    payloads: Sequence[InlineCommentPayload],
    *,
    dry_run: bool,
    debug: Callable[[int, str], None],
) -> InlineCommentPostResult:
    if not payloads:
        if dry_run:
            debug(1, "DRY_RUN: no inline findings to post")
        return InlineCommentPostResult(attempted_count=0, posted_count=0, dry_run=dry_run)

    posted_count = 0
    for payload in payloads:
        if dry_run:
            debug(
                1,
                (f"DRY_RUN: would POST /comments for {payload.path}:{payload.line}"),
            )
            continue

        github_client.post_inline_comment(pr, payload, head_sha=head_sha)
        posted_count += 1

    return InlineCommentPostResult(
        attempted_count=len(payloads),
        posted_count=(0 if dry_run else posted_count),
        dry_run=dry_run,
    )
