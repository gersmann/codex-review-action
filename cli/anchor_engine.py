from __future__ import annotations

from .patch_parser import ParsedPatch, parse_patch


def build_anchor_maps(changed_files: list) -> dict[str, ParsedPatch]:
    """Build anchor maps for valid lines and positions from changed files."""
    maps: dict[str, ParsedPatch] = {}
    for f in changed_files:
        patch = getattr(f, "patch", None)
        filename = getattr(f, "filename", None)
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


def resolve_range(
    path: str,
    requested_start: int,
    requested_end: int,
    has_suggestion: bool,
    file_maps: ParsedPatch,
    max_suggestion_span: int = 5,
) -> dict | None:
    """Resolve the model-provided range to a deterministic anchor.

    Returns a dict with keys: kind ('single'|'range'), line, start_line, end_line, allow_suggestion (bool).
    Returns None if no suitable anchor exists in the diff.
    """
    if requested_start <= 0:
        return None
    if requested_end <= 0:
        requested_end = requested_start
    if requested_end < requested_start:
        requested_start, requested_end = requested_end, requested_start

    valid = set(file_maps.valid_head_lines)
    added = set(file_maps.added_head_lines)
    content = file_maps.content_by_head_line
    hunks = file_maps.hunks

    # Candidate pools (non-blank first)
    added_nb = _nonblank(added, content)
    valid_nb = _nonblank(valid, content)

    # Prefer added non-blank near requested_start; then any valid non-blank
    start_final = _nearest_line(requested_start, added_nb) or _nearest_line(
        requested_start, valid_nb
    )
    end_final = _nearest_line(requested_end, added_nb) or _nearest_line(requested_end, valid_nb)

    if start_final is None and end_final is None:
        return None
    if start_final is None:
        start_final = end_final
    if end_final is None:
        end_final = start_final

    # Narrow types for mypy
    assert start_final is not None and end_final is not None
    start_i = int(start_final)
    end_i = int(end_final)

    if start_i > end_i:
        start_i, end_i = end_i, start_i

    # Decide if we can post a range suggestion
    contiguous = (
        _same_hunk(start_i, end_i, hunks)
        and all(line_num in valid for line_num in range(start_i, end_i + 1))
        and (end_i - start_i + 1) <= max_suggestion_span
    )

    if has_suggestion and contiguous and start_i != end_i:
        return {
            "kind": "range",
            "start_line": start_i,
            "end_line": end_i,
            "allow_suggestion": True,
        }

    # Single-line anchor
    return {
        "kind": "single",
        "line": start_i,
        "allow_suggestion": False,  # only ranges allow suggestions
    }
