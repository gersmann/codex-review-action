from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text_atomic(path: Path, content: str) -> None:
    """Write text to `path` atomically using a same-directory temp file."""
    temp_path: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
