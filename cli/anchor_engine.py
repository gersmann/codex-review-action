from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FileAnchorMaps:
    valid_head_lines: set[int]
    added_head_lines: set[int]
    content_by_head_line: dict[int, str]
    positions_by_head_line: dict[int, int]
    hunks: list[tuple[int, int]]  # inclusive head line ranges observed in this patch


def _parse_patch_maps(
    patch: str,
) -> tuple[set[int], set[int], dict[int, str], list[tuple[int, int]]]:
    """Parse unified diff patch into maps needed for anchoring.

    Returns (valid_head_lines, added_head_lines, content_by_head_line, hunks)
    where valid_head_lines includes context (' ') and added ('+') lines.
    Hunks are inclusive ranges of head lines seen within each @@ block.
    """
    valid: set[int] = set()
    added: set[int] = set()
    content: dict[int, str] = {}
    hunks: list[tuple[int, int]] = []

    i_new = 0
    in_hunk = False
    hunk_min = None
    hunk_max = None

    for raw in patch.splitlines():
        if raw.startswith("@@"):
            if in_hunk and hunk_min is not None and hunk_max is not None and hunk_max >= hunk_min:
                hunks.append((hunk_min, hunk_max))
            in_hunk = True
            hunk_min = None
            hunk_max = None
            # parse header to set i_new start index
            try:
                header = raw.split("@@")[1].strip()
            except IndexError:
                header = raw
            parts = header.split()
            plus = next((t for t in parts if t.startswith("+")), "+0,0")
            try:
                i_new = int(plus[1:].split(",")[0]) - 1
            except Exception:
                i_new = 0
            continue

        if not in_hunk or not raw:
            continue

        tag = raw[0]
        text = raw[1:]

        if tag == " ":
            i_new += 1
            valid.add(i_new)
            content[i_new] = text
        elif tag == "+":
            i_new += 1
            valid.add(i_new)
            added.add(i_new)
            content[i_new] = text
        elif tag == "-":
            # removed line, doesn't advance head line
            pass

        if tag in (" ", "+"):
            if hunk_min is None or i_new < hunk_min:
                hunk_min = i_new
            if hunk_max is None or i_new > hunk_max:
                hunk_max = i_new

    if in_hunk and hunk_min is not None and hunk_max is not None and hunk_max >= hunk_min:
        hunks.append((hunk_min, hunk_max))

    return valid, added, content, hunks


def build_maps(changed_files: list) -> dict[str, FileAnchorMaps]:
    maps: dict[str, FileAnchorMaps] = {}
    for f in changed_files:
        patch = getattr(f, "patch", None)
        filename = getattr(f, "filename", None)
        if not patch or not filename:
            continue
        valid, added, content, hunks = _parse_patch_maps(patch)
        # positions_by_head_line can be empty; only used for optional diagnostics
        # Build simple position map by counting lines inside hunks
        positions: dict[int, int] = {}
        pos = 0
        i_new = 0
        in_hunk = False
        for raw in patch.splitlines():
            if raw.startswith("@@"):
                in_hunk = True
                pos = 0
                try:
                    header = raw.split("@@")[1].strip()
                except IndexError:
                    header = raw
                parts = header.split()
                plus = next((t for t in parts if t.startswith("+")), "+0,0")
                try:
                    i_new = int(plus[1:].split(",")[0]) - 1
                except Exception:
                    i_new = 0
                continue
            if not in_hunk:
                continue
            tag = raw[0]
            pos += 1
            if tag == " ":
                i_new += 1
                if i_new in valid:
                    positions[i_new] = pos
            elif tag == "+":
                i_new += 1
                if i_new in valid:
                    positions[i_new] = pos
            elif tag == "-":
                # removed
                pass

        maps[filename] = FileAnchorMaps(
            valid_head_lines=valid,
            added_head_lines=added,
            content_by_head_line=content,
            positions_by_head_line=positions,
            hunks=hunks,
        )
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
    file_maps: FileAnchorMaps,
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
