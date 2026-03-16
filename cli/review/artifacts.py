from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReviewArtifacts:
    repo_root: Path
    context_dir_name: str

    @property
    def base_dir(self) -> Path:
        return (self.repo_root / self.context_dir_name).resolve()

    @property
    def pr_metadata_path(self) -> Path:
        return self.base_dir / "pr.md"

    @property
    def review_comments_path(self) -> Path:
        return self.base_dir / "review_comments.md"

    @property
    def anchor_maps_path(self) -> Path:
        return self.base_dir / "anchor_maps.json"

    def relative_to_repo_root(self, path: Path) -> Path:
        try:
            return path.relative_to(self.repo_root)
        except ValueError:
            return Path(self.context_dir_name) / path.name
