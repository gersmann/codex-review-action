from __future__ import annotations

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
            if in_hunk and hunk_min is not None and hunk_max is not None and hunk_max >= hunk_min:
                parsed.hunks.append((hunk_min, hunk_max))

            in_hunk = True
            hunk_min = None
            hunk_max = None
            pos_in_patch = 0

            try:
                header = line.split("@@")[1].strip()
            except IndexError:
                header = line

            parts = header.split()
            plus = next((t for t in parts if t.startswith("+")), "+0,0")
            try:
                i_new = int(plus[1:].split(",")[0]) - 1
            except (ValueError, IndexError):
                i_new = 0
            continue

        if not in_hunk or not line:
            continue

        pos_in_patch += 1
        tag = line[0]
        text = line[1:]

        if tag == " ":
            i_new += 1
            parsed.valid_head_lines.add(i_new)
            parsed.content_by_head_line[i_new] = text
            parsed.positions_by_head_line[i_new] = pos_in_patch
        elif tag == "+":
            i_new += 1
            parsed.valid_head_lines.add(i_new)
            parsed.added_head_lines.add(i_new)
            parsed.content_by_head_line[i_new] = text
            parsed.positions_by_head_line[i_new] = pos_in_patch

        if tag in (" ", "+"):
            if hunk_min is None or i_new < hunk_min:
                hunk_min = i_new
            if hunk_max is None or i_new > hunk_max:
                hunk_max = i_new

    if in_hunk and hunk_min is not None and hunk_max is not None and hunk_max >= hunk_min:
        parsed.hunks.append((hunk_min, hunk_max))

    return parsed


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
            # Header looks like: @@ -a,b +c,d @@ optional
            try:
                header = raw.split("@@")[1]
            except IndexError:
                header = raw
            tokens = header.strip().split()
            plus = next((t for t in tokens if t.startswith("+")), "+0,0")
            minus = next((t for t in tokens if t.startswith("-")), "-0,0")
            try:
                i_new = int(plus[1:].split(",")[0]) - 1
                i_old = int(minus[1:].split(",")[0]) - 1
            except (ValueError, IndexError):
                i_new = i_old = 0
            lines_out.append(fmt(None, None, "@", raw))
            continue

        if not raw:
            lines_out.append(raw)
            continue

        tag = raw[0]
        text = raw[1:]
        if tag == " ":
            i_old += 1
            i_new += 1
            lines_out.append(fmt(i_old, i_new, tag, text))
        elif tag == "+":
            i_new += 1
            lines_out.append(fmt(None, i_new, tag, text))
        elif tag == "-":
            i_old += 1
            lines_out.append(fmt(i_old, None, tag, text))
        else:
            # Unknown tag; just echo
            lines_out.append(raw)

    return "\n".join(lines_out)


def to_relative_path(abs_path: str, repo_root: Path) -> str:
    """Convert an absolute path to a relative path under repo_root."""
    try:
        return str(Path(abs_path).resolve().relative_to(repo_root))
    except Exception:
        return abs_path.lstrip("./")
