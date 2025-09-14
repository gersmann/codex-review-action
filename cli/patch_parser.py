from __future__ import annotations

from .exceptions import PatchParsingError


def parse_valid_head_lines_from_patch(patch: str) -> set[int]:
    """Parse a unified diff patch and return valid line numbers in the HEAD version."""
    valid: set[int] = set()
    i_old = i_new = 0

    for line in patch.splitlines():
        if line.startswith("@@"):
            # Header looks like: @@ -a,b +c,d @@ optional
            try:
                header = line.split("@@")[1]
            except IndexError:
                header = line
            tokens = header.strip().split()

            # Find tokens starting with '+' and '-'
            plus = next((t for t in tokens if t.startswith("+")), "+0,0")
            minus = next((t for t in tokens if t.startswith("-")), "-0,0")

            try:
                i_new = int(plus[1:].split(",")[0]) - 1
                i_old = int(minus[1:].split(",")[0]) - 1
            except (ValueError, IndexError):
                i_new = i_old = 0
            continue

        if not line:
            continue

        tag = line[0]
        if tag == " ":
            # Context line
            i_old += 1
            i_new += 1
            valid.add(i_new)
        elif tag == "+":
            # Added line
            i_new += 1
            valid.add(i_new)
        elif tag == "-":
            # Removed line
            i_old += 1

    return valid


def compute_position_from_patch(patch: str, target_head_line: int) -> int | None:
    """Compute the position in the patch for a given line number in HEAD."""
    pos = 0
    i_old = i_new = 0
    in_hunk = False

    for line in patch.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            try:
                header = line.split("@@")[1]
            except IndexError:
                header = line
            tokens = header.strip().split()

            plus = next((t for t in tokens if t.startswith("+")), "+0,0")
            minus = next((t for t in tokens if t.startswith("-")), "-0,0")

            try:
                i_new = int(plus[1:].split(",")[0]) - 1
                i_old = int(minus[1:].split(",")[0]) - 1
            except (ValueError, IndexError):
                i_new = i_old = 0
            continue

        if not in_hunk or not line:
            continue

        tag = line[0]
        pos += 1

        if tag == " ":
            i_old += 1
            i_new += 1
            if i_new == target_head_line:
                return pos
        elif tag == "+":
            i_new += 1
            if i_new == target_head_line:
                return pos
        elif tag == "-":
            i_old += 1

    return None


def build_anchor_maps(changed_files: list) -> tuple[dict[str, set[int]], dict[str, dict[int, int]]]:
    """Build anchor maps for valid lines and positions from changed files."""
    valid_lines_by_path: dict[str, set[int]] = {}
    position_by_path: dict[str, dict[int, int]] = {}

    for file in changed_files:
        if not file.patch:
            continue

        try:
            valid_lines_by_path[file.filename] = parse_valid_head_lines_from_patch(file.patch)
            pos_map: dict[int, int] = {}

            for line_num in valid_lines_by_path[file.filename]:
                pos = compute_position_from_patch(file.patch, line_num)
                if pos is not None:
                    pos_map[line_num] = pos

            position_by_path[file.filename] = pos_map

        except Exception as e:
            raise PatchParsingError(f"Failed to parse patch for {file.filename}: {e}") from e

    return valid_lines_by_path, position_by_path
