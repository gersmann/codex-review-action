from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedPatch:
    """Dataclass to hold all information about a parsed patch."""

    valid_head_lines: set[int] = field(default_factory=set)
    added_head_lines: set[int] = field(default_factory=set)
    content_by_head_line: dict[int, str] = field(default_factory=dict)
    positions_by_head_line: dict[int, int] = field(default_factory=dict)
    hunks: list[tuple[int, int]] = field(default_factory=list)


def _extract_hunk_header(line: str) -> str:
    try:
        return line.split("@@")[1].strip()
    except IndexError:
        return line


def _find_hunk_token(tokens: list[str], prefix: str, default: str) -> str:
    return next((token for token in tokens if token.startswith(prefix)), default)


def _read_hunk_start(token: str) -> int:
    try:
        return int(token[1:].split(",")[0])
    except (ValueError, IndexError):
        return 0


def _extract_hunk_starts(line: str) -> tuple[int, int]:
    tokens = _extract_hunk_header(line).split()
    return (
        _read_hunk_start(_find_hunk_token(tokens, "+", "+0,0")),
        _read_hunk_start(_find_hunk_token(tokens, "-", "-0,0")),
    )


def _append_hunk_if_valid(parsed: ParsedPatch, hunk_min: int | None, hunk_max: int | None) -> None:
    if hunk_min is None or hunk_max is None:
        return
    if hunk_max < hunk_min:
        return
    parsed.hunks.append((hunk_min, hunk_max))


def _record_head_line(
    parsed: ParsedPatch,
    *,
    head_line: int,
    text: str,
    pos_in_patch: int,
    is_added: bool,
) -> None:
    parsed.valid_head_lines.add(head_line)
    parsed.content_by_head_line[head_line] = text
    parsed.positions_by_head_line[head_line] = pos_in_patch
    if is_added:
        parsed.added_head_lines.add(head_line)


def _expand_hunk_bounds(
    head_line: int,
    hunk_min: int | None,
    hunk_max: int | None,
) -> tuple[int, int]:
    next_min = head_line if hunk_min is None or head_line < hunk_min else hunk_min
    next_max = head_line if hunk_max is None or head_line > hunk_max else hunk_max
    return next_min, next_max


def parse_patch(patch: str) -> ParsedPatch:
    """
    Parse a unified diff patch and return a ParsedPatch object containing all relevant data.
    This function iterates through the patch a single time to be efficient.
    """
    parsed = ParsedPatch()

    i_new = 0
    in_hunk = False
    hunk_min = None
    hunk_max = None
    pos_in_patch = 0

    for line in patch.splitlines():
        if line.startswith("@@"):
            if in_hunk:
                _append_hunk_if_valid(parsed, hunk_min, hunk_max)

            in_hunk = True
            hunk_min = None
            hunk_max = None
            pos_in_patch = 0
            head_start, _ = _extract_hunk_starts(line)
            i_new = head_start - 1 if head_start > 0 else 0
            continue

        if not in_hunk or not line:
            continue

        pos_in_patch += 1
        tag = line[0]
        text = line[1:]

        if tag in {" ", "+"}:
            i_new += 1
            _record_head_line(
                parsed,
                head_line=i_new,
                text=text,
                pos_in_patch=pos_in_patch,
                is_added=(tag == "+"),
            )
            hunk_min, hunk_max = _expand_hunk_bounds(i_new, hunk_min, hunk_max)

    if in_hunk:
        _append_hunk_if_valid(parsed, hunk_min, hunk_max)

    return parsed


def _annotate_body_line(
    raw_line: str,
    *,
    i_old: int,
    i_new: int,
    formatter: Callable[[int | None, int | None, str, str], str],
) -> tuple[str, int, int]:
    tag = raw_line[0]
    text = raw_line[1:]
    if tag == " ":
        i_old += 1
        i_new += 1
        return formatter(i_old, i_new, tag, text), i_old, i_new
    if tag == "+":
        i_new += 1
        return formatter(None, i_new, tag, text), i_old, i_new
    if tag == "-":
        i_old += 1
        return formatter(i_old, None, tag, text), i_old, i_new
    return raw_line, i_old, i_new


def annotate_patch_with_line_numbers(patch: str) -> str:
    """Return a human-friendly annotated diff with BASE and HEAD line numbers.

    Format (fixed-width columns):
      BASE   HEAD   TAG  CONTENT
      0123          -    removed line
             0456   +    added line
      0123  0456         context line

    This is only for debugging/inspection and is NOT sent to the model.
    """
    lines_out: list[str] = []
    i_old = i_new = 0

    def fmt(old: int | None, new: int | None, tag: str, text: str) -> str:
        b = f"{old:>6}" if isinstance(old, int) and old > 0 else "      "
        h = f"{new:>6}" if isinstance(new, int) and new > 0 else "      "
        t = tag if tag in {"+", "-", " ", "@"} else "?"
        return f"{b}  {h}   {t}  {text}"

    for raw in patch.splitlines():
        if raw.startswith("@@"):
            head_start, base_start = _extract_hunk_starts(raw)
            i_new = head_start - 1 if head_start > 0 else 0
            i_old = base_start - 1 if base_start > 0 else 0
            lines_out.append(fmt(None, None, "@", raw))
            continue

        if not raw:
            lines_out.append(raw)
            continue

        rendered, i_old, i_new = _annotate_body_line(
            raw,
            i_old=i_old,
            i_new=i_new,
            formatter=fmt,
        )
        lines_out.append(rendered)

    return "\n".join(lines_out)


def to_relative_path(abs_path: str, repo_root: Path) -> str:
    """Convert an absolute path to a relative path under repo_root."""
    try:
        return str(Path(abs_path).resolve().relative_to(repo_root))
    except Exception:
        return abs_path.lstrip("./")
