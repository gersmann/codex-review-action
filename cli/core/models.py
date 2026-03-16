from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .exceptions import ReviewContractError

if TYPE_CHECKING:
    from .github_types import IssueCommentLikeProtocol, ReviewCommentLikeProtocol


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
            return None

        event_name = str(payload.get("event_name") or "")
        if comment_id <= 0 or not event_name:
            return None
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
    def from_finding(cls, finding: Mapping[str, Any]) -> FindingLocation | None:
        loc = finding.get("code_location")
        if not isinstance(loc, Mapping):
            return None

        abs_path_raw = loc.get("absolute_file_path")
        abs_path = abs_path_raw.strip() if isinstance(abs_path_raw, str) else ""
        rng = loc.get("line_range")
        if not isinstance(rng, Mapping):
            return None

        start = _as_int(rng.get("start"), 0)
        end = _as_int(rng.get("end"), start)
        if end <= 0 and start > 0:
            end = start
        if not abs_path or start <= 0:
            return None
        return cls(abs_path, start, end)

    @classmethod
    def from_review_finding(cls, finding: ReviewFinding) -> FindingLocation:
        return cls(
            absolute_file_path=finding.code_location.absolute_file_path,
            start_line=finding.code_location.start_line,
            end_line=finding.code_location.end_line,
        )


@dataclass(frozen=True)
class ReviewFindingLocation:
    absolute_file_path: str
    start_line: int
    end_line: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReviewFindingLocation | None:
        base = FindingLocation.from_finding({"code_location": payload})
        if base is None:
            return None
        return cls(
            absolute_file_path=base.absolute_file_path,
            start_line=base.start_line,
            end_line=base.end_line,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "absolute_file_path": self.absolute_file_path,
            "line_range": {
                "start": self.start_line,
                "end": self.end_line,
            },
        }


@dataclass(frozen=True)
class ReviewFinding:
    title: str
    body: str
    confidence_score: float | None
    priority: int | None
    code_location: ReviewFindingLocation

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReviewFinding:
        required_fields = {"title", "body", "confidence_score", "priority", "code_location"}
        missing_fields = sorted(required_fields - set(payload.keys()))
        if missing_fields:
            raise ReviewContractError(
                "Review finding missing required fields: " + ", ".join(missing_fields)
            )

        title_raw = payload.get("title")
        if not isinstance(title_raw, str):
            raise ReviewContractError("Review finding field 'title' must be a string")

        body_raw = payload.get("body")
        if not isinstance(body_raw, str):
            raise ReviewContractError("Review finding field 'body' must be a string")

        confidence_raw = payload.get("confidence_score")
        if confidence_raw is not None and not isinstance(confidence_raw, (int, float)):
            raise ReviewContractError(
                "Review finding field 'confidence_score' must be a number or null"
            )
        confidence_score = (
            float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None
        )

        priority_raw = payload.get("priority")
        if priority_raw is not None and not isinstance(priority_raw, int):
            raise ReviewContractError("Review finding field 'priority' must be an integer or null")
        priority = int(priority_raw) if isinstance(priority_raw, int) else None

        code_location_raw = payload.get("code_location")
        if not isinstance(code_location_raw, Mapping):
            raise ReviewContractError("Review finding field 'code_location' must be an object")
        code_location = ReviewFindingLocation.from_mapping(code_location_raw)
        if code_location is None:
            raise ReviewContractError("Review finding field 'code_location' is invalid")

        return cls(
            title=title_raw,
            body=body_raw,
            confidence_score=confidence_score,
            priority=priority,
            code_location=code_location,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "confidence_score": self.confidence_score,
            "priority": self.priority,
            "code_location": self.code_location.as_dict(),
        }


@dataclass(frozen=True)
class ExistingReviewComment:
    """Structured inline review comment used for local dedupe."""

    id: str
    path: str
    line: int
    body: str
    current_code: str


@dataclass(frozen=True)
class CarriedForwardReviewComment:
    """Prior Codex review comment re-adjudicated as still applicable."""

    comment_id: str
    current_evidence: str


@dataclass(frozen=True)
class IssueCommentSnapshot:
    body: str
    created_at: str
    author: str = ""

    @classmethod
    def from_issue_comment(cls, comment: IssueCommentLikeProtocol) -> IssueCommentSnapshot:
        return cls(
            body=comment.body if isinstance(comment.body, str) else "",
            created_at=str(comment.created_at),
            author=comment.user.login if comment.user is not None else "",
        )


@dataclass(frozen=True)
class ReviewCommentSnapshot:
    body: str
    path: str
    line: int | None
    original_line: int | None
    author: str = ""
    created_at: str = ""
    diff_hunk: str = ""
    commit_id: str = ""
    in_reply_to_id: int | None = None

    @property
    def prompt_line(self) -> int | None:
        return self.line if self.line is not None else self.original_line

    @classmethod
    def from_review_comment(cls, comment: ReviewCommentLikeProtocol) -> ReviewCommentSnapshot:
        author_value = comment.user.login if comment.user is not None else None
        return cls(
            body=comment.body.strip() if isinstance(comment.body, str) else "",
            path=comment.path if isinstance(comment.path, str) else "",
            line=comment.line if isinstance(comment.line, int) else None,
            original_line=comment.original_line if isinstance(comment.original_line, int) else None,
            author=author_value if isinstance(author_value, str) else "",
            created_at=str(comment.created_at) if comment.created_at is not None else "",
            diff_hunk=comment.diff_hunk if isinstance(comment.diff_hunk, str) else "",
            commit_id=comment.commit_id if isinstance(comment.commit_id, str) else "",
            in_reply_to_id=comment.in_reply_to_id
            if isinstance(comment.in_reply_to_id, int)
            else None,
        )


@dataclass(frozen=True)
class ReviewThreadComment:
    """Normalized review-thread comment snapshot from GraphQL."""

    id: str
    body: str
    path: str
    line: int | None
    original_line: int | None
    author: str = ""

    @property
    def prompt_line(self) -> int | None:
        return self.line if self.line is not None else self.original_line


@dataclass(frozen=True)
class ReviewThreadSnapshot:
    """Normalized review thread snapshot with resolution state."""

    id: str
    is_resolved: bool
    comments: list[ReviewThreadComment]


@dataclass(frozen=True)
class UnresolvedReviewComment:
    """Normalized review-comment context for unresolved thread prompts."""

    id: str
    body: str
    path: str
    line: int | None
    original_line: int | None
    author: str = ""

    @property
    def prompt_line(self) -> int | None:
        return self.line if self.line is not None else self.original_line


@dataclass(frozen=True)
class UnresolvedReviewThread:
    """Normalized unresolved review thread used by edit mode."""

    id: str
    comments: list[UnresolvedReviewComment]


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
    overall_confidence_score: float | None
    findings: list[ReviewFinding]
    carried_forward: list[CarriedForwardReviewComment] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReviewRunResult:
        required_fields = {
            "findings",
            "carried_forward",
            "overall_correctness",
            "overall_explanation",
            "overall_confidence_score",
        }
        missing_fields = sorted(required_fields - set(payload.keys()))
        if missing_fields:
            raise ReviewContractError(
                "Review output missing required fields: " + ", ".join(missing_fields)
            )

        findings_raw = payload.get("findings")
        if not isinstance(findings_raw, list):
            raise ReviewContractError("Review output field 'findings' must be an array")
        findings: list[ReviewFinding] = []
        for index, item in enumerate(findings_raw):
            if not isinstance(item, Mapping):
                raise ReviewContractError(
                    f"Review output finding at index {index} must be an object"
                )
            findings.append(ReviewFinding.from_mapping(item))

        overall_correctness_raw = payload.get("overall_correctness")
        if not isinstance(overall_correctness_raw, str):
            raise ReviewContractError("Review output field 'overall_correctness' must be a string")
        overall_correctness = overall_correctness_raw

        overall_explanation_raw = payload.get("overall_explanation")
        if not isinstance(overall_explanation_raw, str):
            raise ReviewContractError("Review output field 'overall_explanation' must be a string")
        overall_explanation = overall_explanation_raw

        confidence_raw = payload.get("overall_confidence_score")
        if confidence_raw is not None and not isinstance(confidence_raw, (int, float)):
            raise ReviewContractError(
                "Review output field 'overall_confidence_score' must be a number or null"
            )
        overall_confidence_score = (
            float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None
        )
        carried_forward_raw = payload.get("carried_forward")
        if not isinstance(carried_forward_raw, list):
            raise ReviewContractError("Review output field 'carried_forward' must be an array")
        carried_forward: list[CarriedForwardReviewComment] = []
        for index, item in enumerate(carried_forward_raw):
            if not isinstance(item, Mapping):
                raise ReviewContractError(
                    f"Review output field 'carried_forward' item at index {index} must be an object"
                )
            comment_id = item.get("comment_id")
            if not isinstance(comment_id, str):
                raise ReviewContractError(
                    "Review output field 'carried_forward' "
                    f"item at index {index} must include string field 'comment_id'"
                )
            current_evidence = item.get("current_evidence")
            if not isinstance(current_evidence, str):
                raise ReviewContractError(
                    "Review output field 'carried_forward' "
                    f"item at index {index} must include string field 'current_evidence'"
                )
            carried_forward.append(
                CarriedForwardReviewComment(
                    comment_id=comment_id,
                    current_evidence=current_evidence,
                )
            )
        return cls(
            overall_correctness=overall_correctness,
            overall_explanation=overall_explanation,
            overall_confidence_score=overall_confidence_score,
            findings=findings,
            carried_forward=carried_forward,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall_correctness": self.overall_correctness,
            "overall_explanation": self.overall_explanation,
            "overall_confidence_score": self.overall_confidence_score,
            "findings": [finding.as_dict() for finding in self.findings],
            "carried_forward": [
                {
                    "comment_id": item.comment_id,
                    "current_evidence": item.current_evidence,
                }
                for item in self.carried_forward
            ],
        }

    @property
    def carried_forward_comment_ids(self) -> list[str]:
        return [item.comment_id for item in self.carried_forward]


REVIEW_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "confidence_score": {"type": ["number", "null"]},
                    "priority": {"type": ["integer", "null"]},
                    "code_location": {
                        "type": "object",
                        "properties": {
                            "absolute_file_path": {"type": "string"},
                            "line_range": {
                                "type": "object",
                                "properties": {
                                    "start": {"type": "integer"},
                                    "end": {"type": "integer"},
                                },
                                "required": ["start", "end"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["absolute_file_path", "line_range"],
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "title",
                    "body",
                    "confidence_score",
                    "priority",
                    "code_location",
                ],
                "additionalProperties": False,
            },
        },
        "carried_forward": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string"},
                    "current_evidence": {"type": "string"},
                },
                "required": ["comment_id", "current_evidence"],
                "additionalProperties": False,
            },
        },
        "overall_correctness": {"type": "string"},
        "overall_explanation": {"type": "string"},
        "overall_confidence_score": {"type": ["number", "null"]},
    },
    "required": [
        "findings",
        "carried_forward",
        "overall_correctness",
        "overall_explanation",
        "overall_confidence_score",
    ],
    "additionalProperties": False,
}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
