from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    Strict,
    StrictStr,
    TypeAdapter,
    ValidationError,
    with_config,
)
from typing_extensions import TypedDict

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


# Input tolerates extra fields; generated output is a closed, fully populated object.
_REVIEW_CONFIG = ConfigDict(
    extra="ignore",
    json_schema_extra={"additionalProperties": False},
    json_schema_serialization_defaults_required=True,
)


@with_config(_REVIEW_CONFIG)
class _ReviewLineRange(TypedDict):
    start: int
    end: int


@with_config(_REVIEW_CONFIG)
class _ReviewLocationPayload(TypedDict):
    absolute_file_path: str
    line_range: _ReviewLineRange


def _boolean_as_number(value: object) -> object:
    return int(value) if isinstance(value, bool) else value


def _review_object(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("must be an object")
    return dict(value)


def _review_location(value: object) -> ReviewFindingLocation:
    location = ReviewFindingLocation.from_mapping(_review_object(value))
    if location is None:
        raise ValueError("invalid finding location")
    return location


_ReviewFloat = Annotated[float, Strict(), BeforeValidator(_boolean_as_number)]
_ReviewInt = Annotated[int, Strict(), BeforeValidator(_boolean_as_number)]


@dataclass(frozen=True)
class ReviewFinding:
    __pydantic_config__: ClassVar[ConfigDict] = _REVIEW_CONFIG

    title: StrictStr
    body: StrictStr
    confidence_score: _ReviewFloat | None
    priority: _ReviewInt | None
    code_location: Annotated[
        ReviewFindingLocation,
        BeforeValidator(_review_location),
        PlainSerializer(ReviewFindingLocation.as_dict, return_type=_ReviewLocationPayload),
    ]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReviewFinding:
        missing_fields = sorted({item.name for item in fields(cls)} - payload.keys())
        if missing_fields:
            raise ReviewContractError(
                "Review finding missing required fields: " + ", ".join(missing_fields)
            )
        try:
            return _FINDING_ADAPTER.validate_python(dict(payload))
        except ValidationError as exc:
            raise ReviewContractError(f"Invalid review finding: {exc}") from exc

    def as_dict(self) -> dict[str, Any]:
        return _FINDING_ADAPTER.dump_python(self)


@dataclass(frozen=True)
class PriorCodexReviewComment:
    """Unresolved Codex-authored review thread comment reused on reruns."""

    id: str
    thread_id: str
    path: str
    line: int
    body: str
    current_code: str
    is_currently_applicable: bool


@dataclass(frozen=True)
class CarriedForwardReviewComment:
    """Prior Codex review comment re-adjudicated as still applicable."""

    __pydantic_config__: ClassVar[ConfigDict] = _REVIEW_CONFIG

    comment_id: StrictStr
    current_evidence: StrictStr


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

    __pydantic_config__: ClassVar[ConfigDict] = _REVIEW_CONFIG

    overall_correctness: StrictStr
    overall_explanation: StrictStr
    overall_confidence_score: _ReviewFloat | None
    findings: Annotated[
        list[Annotated[ReviewFinding, BeforeValidator(_review_object)]], Field(strict=True)
    ]
    carried_forward: Annotated[
        list[Annotated[CarriedForwardReviewComment, BeforeValidator(_review_object)]],
        Field(strict=True),
    ] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReviewRunResult:
        # Internal constructors may use defaults; external output must include every field.
        missing_fields = sorted({item.name for item in fields(cls)} - payload.keys())
        if missing_fields:
            raise ReviewContractError(
                "Review output missing required fields: " + ", ".join(missing_fields)
            )
        try:
            return _REVIEW_ADAPTER.validate_python(dict(payload))
        except ValidationError as exc:
            raise ReviewContractError(f"Invalid review output: {exc}") from exc

    def as_dict(self) -> dict[str, Any]:
        return _REVIEW_ADAPTER.dump_python(self)

    @property
    def carried_forward_comment_ids(self) -> list[str]:
        return [item.comment_id for item in self.carried_forward]


_FINDING_ADAPTER = TypeAdapter(ReviewFinding)
_REVIEW_ADAPTER = TypeAdapter(ReviewRunResult)
REVIEW_OUTPUT_SCHEMA: dict[str, object] = _REVIEW_ADAPTER.json_schema(mode="serialization")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
