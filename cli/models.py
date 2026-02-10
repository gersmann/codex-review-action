from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommentContext:
    """Context for comment-triggered edit commands."""

    id: int
    event_name: str
    author: str = ""
    body: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> CommentContext | None:
        if payload is None:
            return None
        try:
            comment_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            comment_id = 0

        event_name = str(payload.get("event_name") or "")
        author = str(payload.get("author") or "")
        body = str(payload.get("body") or "")
        return cls(id=comment_id, event_name=event_name, author=author, body=body)


@dataclass(frozen=True)
class FindingLocation:
    """Normalized finding location values parsed from model output."""

    absolute_file_path: str
    start_line: int
    end_line: int

    @classmethod
    def from_finding(cls, finding: Mapping[str, Any]) -> FindingLocation:
        loc = finding.get("code_location")
        if not isinstance(loc, Mapping):
            return cls("", 0, 0)

        abs_path = str(loc.get("absolute_file_path") or "").strip()
        rng = loc.get("line_range")
        if not isinstance(rng, Mapping):
            return cls(abs_path, 0, 0)

        start = _as_int(rng.get("start"), 0)
        end = _as_int(rng.get("end"), start)
        if end <= 0 and start > 0:
            end = start
        return cls(abs_path, start, end)


@dataclass(frozen=True)
class ExistingReviewComment:
    """Structured inline review comment used for local dedupe."""

    path: str
    line: int
    body: str


@dataclass(frozen=True)
class InlineCommentPayload:
    """Payload for posting a GitHub inline review comment."""

    body: str
    path: str
    side: str = "RIGHT"
    line: int = 0
    start_line: int | None = None
    start_side: str = "RIGHT"

    def to_request_payload(self, head_sha: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "body": self.body,
            "path": self.path,
            "side": self.side,
            "commit_id": head_sha,
            "line": int(self.line),
        }
        if self.start_line is not None:
            payload["start_line"] = int(self.start_line)
            payload["start_side"] = self.start_side
        return payload


@dataclass(frozen=True)
class ReviewRunResult:
    """Typed view of model output for a review run."""

    overall_correctness: str
    overall_explanation: str
    findings: list[dict[str, Any]]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReviewRunResult:
        findings_raw = payload.get("findings")
        findings: list[dict[str, Any]] = []
        if isinstance(findings_raw, list):
            for item in findings_raw:
                if isinstance(item, Mapping):
                    findings.append(dict(item))

        overall_correctness = str(payload.get("overall_correctness") or "")
        overall_explanation = str(payload.get("overall_explanation") or "")
        return cls(
            overall_correctness=overall_correctness,
            overall_explanation=overall_explanation,
            findings=findings,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall_correctness": self.overall_correctness,
            "overall_explanation": self.overall_explanation,
            "findings": self.findings,
        }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
