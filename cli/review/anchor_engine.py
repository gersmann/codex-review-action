from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from ..core.github_types import ChangedFileProtocol
from .patch_parser import ParsedPatch, parse_patch


@dataclass(frozen=True)
class SingleAnchor:
    kind: Literal["single"]
    line: int
    allow_suggestion: Literal[False] = False


@dataclass(frozen=True)
class RangeAnchor:
    kind: Literal["range"]
    start_line: int
    end_line: int
    allow_suggestion: Literal[True] = True


Anchor = SingleAnchor | RangeAnchor


def build_anchor_maps(changed_files: Iterable[ChangedFileProtocol]) -> dict[str, ParsedPatch]:
    """Build anchor maps for valid lines and positions from changed files."""
    maps: dict[str, ParsedPatch] = {}
    for changed_file in changed_files:
        patch = changed_file.patch
        filename = changed_file.filename
        if not patch or not filename:
            continue

        maps[filename] = parse_patch(patch)
    return maps


def _nearest_line(target: int, preferred: list[int]) -> int | None:
    if not preferred:
        return None
    return min(preferred, key=lambda x: (abs(x - target), x))


def _nonblank(lines: set[int], content: dict[int, str]) -> list[int]:
    return [line_num for line_num in lines if str(content.get(line_num, "")).strip() != ""]


def _same_hunk(line_a: int, line_b: int, hunks: list[tuple[int, int]]) -> bool:
    for lo, hi in hunks:
        if lo <= line_a <= hi and lo <= line_b <= hi:
            return True
    return False


def _normalize_requested_range(requested_start: int, requested_end: int) -> tuple[int, int] | None:
    if requested_start <= 0:
        return None
    normalized_end = requested_end if requested_end > 0 else requested_start
    normalized_start = requested_start
    if normalized_end < normalized_start:
        normalized_start, normalized_end = normalized_end, normalized_start
    return normalized_start, normalized_end


def _nearest_nonblank_line(target: int, file_map: ParsedPatch) -> int | None:
    added_nonblank = _nonblank(set(file_map.added_head_lines), file_map.content_by_head_line)
    valid_nonblank = _nonblank(set(file_map.valid_head_lines), file_map.content_by_head_line)
    return _nearest_line(target, added_nonblank) or _nearest_line(target, valid_nonblank)


def _resolve_endpoints(
    *,
    requested_start: int,
    requested_end: int,
    file_map: ParsedPatch,
) -> tuple[int, int] | None:
    start_final = _nearest_nonblank_line(requested_start, file_map)
    end_final = _nearest_nonblank_line(requested_end, file_map)
    if start_final is None and end_final is None:
        return None
    if start_final is None:
        start_final = end_final
    if end_final is None:
        end_final = start_final
    if start_final is None or end_final is None:
        return None

    start_i = int(start_final)
    end_i = int(end_final)
    if start_i > end_i:
        start_i, end_i = end_i, start_i
    return start_i, end_i


def _is_contiguous_valid_range(
    *,
    start_line: int,
    end_line: int,
    valid_lines: set[int],
    hunks: list[tuple[int, int]],
    max_suggestion_span: int,
) -> bool:
    return (
        _same_hunk(start_line, end_line, hunks)
        and all(line_num in valid_lines for line_num in range(start_line, end_line + 1))
        and (end_line - start_line + 1) <= max_suggestion_span
    )


def resolve_range(
    requested_start: int,
    requested_end: int,
    has_suggestion: bool,
    file_map: ParsedPatch,
    max_suggestion_span: int = 5,
) -> Anchor | None:
    """Resolve the model-provided range to a deterministic anchor.

    Returns a typed single-line or range anchor. Returns None if no suitable
    anchor exists in the diff.
    """
    normalized_range = _normalize_requested_range(requested_start, requested_end)
    if normalized_range is None:
        return None
    normalized_start, normalized_end = normalized_range

    endpoints = _resolve_endpoints(
        requested_start=normalized_start,
        requested_end=normalized_end,
        file_map=file_map,
    )
    if endpoints is None:
        return None
    start_i, end_i = endpoints

    if (
        has_suggestion
        and start_i != end_i
        and _is_contiguous_valid_range(
            start_line=start_i,
            end_line=end_i,
            valid_lines=set(file_map.valid_head_lines),
            hunks=file_map.hunks,
            max_suggestion_span=max_suggestion_span,
        )
    ):
        return RangeAnchor(kind="range", start_line=start_i, end_line=end_i)

    # Single-line anchor
    return SingleAnchor(kind="single", line=start_i)
