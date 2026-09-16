from __future__ import annotations

import subprocess
import sys
from collections import UserDict
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from codex.app_server.options import AppServerTurnOptions

from cli.core.exceptions import ReviewContractError
from cli.core.models import (
    REVIEW_OUTPUT_SCHEMA,
    ReviewFinding,
    ReviewFindingLocation,
    ReviewRunResult,
)


def finding_payload() -> dict[str, Any]:
    return {
        "title": "Finding",
        "body": "Details",
        "confidence_score": None,
        "priority": None,
        "code_location": {
            "absolute_file_path": "/tmp/example.py",
            "line_range": {"start": 4, "end": 5},
        },
    }


def review_payload() -> dict[str, Any]:
    return {
        "overall_correctness": "patch is correct",
        "overall_explanation": "Details",
        "overall_confidence_score": None,
        "findings": [finding_payload()],
        "carried_forward": [{"comment_id": "comment-1", "current_evidence": "code"}],
    }


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("4", "6", (4, 6)),
        (4, 0, (4, 4)),
        (4, -1, (4, 4)),
        (4, None, (4, 4)),
        (4, "invalid", (4, 4)),
        (4.9, 2.9, (4, 2)),
        (True, False, (1, 1)),
    ],
)
def test_location_input_normalizes_without_tightening_ranges(
    start: object, end: object, expected: tuple[int, int]
) -> None:
    payload = finding_payload()
    payload["code_location"] = {
        "absolute_file_path": " /tmp/example.py \n",
        "line_range": {"start": start, "end": end},
    }

    location = ReviewFinding.from_mapping(payload).code_location

    assert location == ReviewFindingLocation("/tmp/example.py", *expected)
    assert location.as_dict() == {
        "absolute_file_path": "/tmp/example.py",
        "line_range": {"start": expected[0], "end": expected[1]},
    }


def test_location_missing_end_defaults_to_start() -> None:
    payload = finding_payload()
    payload["code_location"]["line_range"] = {"start": "4"}
    assert ReviewFinding.from_mapping(payload).code_location.end_line == 4


@pytest.mark.parametrize("value", [True, False, 1, -2])
def test_numeric_fields_accept_bool_and_out_of_range_numbers(value: int) -> None:
    payload = review_payload()
    payload["overall_confidence_score"] = value
    payload["findings"][0]["confidence_score"] = value
    payload["findings"][0]["priority"] = value

    result = ReviewRunResult.from_payload(payload)

    assert result.overall_confidence_score == float(value)
    assert type(result.overall_confidence_score) is float
    assert result.findings[0].confidence_score == float(value)
    assert type(result.findings[0].confidence_score) is float
    assert result.findings[0].priority == int(value)
    assert type(result.findings[0].priority) is int


@pytest.mark.parametrize(
    ("field", "value"),
    [("title", 1), ("body", None), ("confidence_score", "0.5"), ("priority", 1.0)],
)
def test_finding_fields_do_not_coerce_other_scalar_types(field: str, value: object) -> None:
    payload = finding_payload()
    payload[field] = value
    with pytest.raises(ReviewContractError):
        ReviewFinding.from_mapping(payload)


@pytest.mark.parametrize("field", ["confidence_score", "priority"])
def test_nullable_finding_fields_are_still_required(field: str) -> None:
    payload = finding_payload()
    del payload[field]
    with pytest.raises(ReviewContractError, match="missing required fields"):
        ReviewFinding.from_mapping(payload)


@pytest.mark.parametrize("field", ["overall_confidence_score", "carried_forward"])
def test_nullable_or_defaulted_result_fields_are_still_required_on_input(field: str) -> None:
    payload = review_payload()
    del payload[field]
    with pytest.raises(ReviewContractError, match="missing required fields"):
        ReviewRunResult.from_payload(payload)


@pytest.mark.parametrize("field", ["findings", "carried_forward"])
def test_result_collections_require_lists(field: str) -> None:
    payload = review_payload()
    payload[field] = ()
    with pytest.raises(ReviewContractError, match="valid list"):
        ReviewRunResult.from_payload(payload)


def test_input_ignores_extra_fields_but_generation_schema_forbids_them() -> None:
    expected = review_payload()
    payload = review_payload()
    payload["extra"] = "ignored"
    payload["findings"][0]["extra"] = "ignored"
    payload["findings"][0]["code_location"]["extra"] = "ignored"
    payload["findings"][0]["code_location"]["line_range"]["extra"] = "ignored"
    payload["carried_forward"][0]["extra"] = "ignored"

    assert ReviewRunResult.from_payload(payload).as_dict() == expected
    assert REVIEW_OUTPUT_SCHEMA["additionalProperties"] is False
    required = REVIEW_OUTPUT_SCHEMA["required"]
    assert isinstance(required, list)
    assert "carried_forward" in required


def test_external_input_accepts_mappings_at_each_object_boundary() -> None:
    expected = review_payload()
    payload = review_payload()
    finding = payload["findings"][0]
    location = finding["code_location"]
    location["line_range"] = UserDict(location["line_range"])
    finding["code_location"] = UserDict(location)
    payload["findings"] = [UserDict(finding)]
    payload["carried_forward"] = [UserDict(payload["carried_forward"][0])]

    assert ReviewRunResult.from_payload(UserDict(payload)).as_dict() == expected


def test_external_input_does_not_accept_preconstructed_findings() -> None:
    payload = review_payload()
    payload["findings"] = [ReviewFinding.from_mapping(finding_payload())]

    with pytest.raises(ReviewContractError, match="must be an object"):
        ReviewRunResult.from_payload(payload)


def test_direct_construction_preserves_flat_locations_defaults_and_frozen_fields() -> None:
    location = ReviewFindingLocation(" untouched ", 4, 0)
    finding = ReviewFinding("Finding", "Details", None, None, location)
    result = ReviewRunResult("patch is correct", "Details", None, [finding])
    another = ReviewRunResult("patch is correct", "Details", None, [])

    assert result.carried_forward == []
    assert result.carried_forward is not another.carried_forward
    assert result.findings[0].code_location.as_dict() == {
        "absolute_file_path": " untouched ",
        "line_range": {"start": 4, "end": 0},
    }
    with pytest.raises(FrozenInstanceError):
        location.__setattr__("end_line", 4)


def test_generated_schema_preserves_closed_wire_objects_and_sdk_transport() -> None:
    options = AppServerTurnOptions(output_schema=REVIEW_OUTPUT_SCHEMA)
    params = options.to_params(thread_id="test-thread", input=[])
    assert params.model_dump(by_alias=True)["outputSchema"] == REVIEW_OUTPUT_SCHEMA

    def resolve(schema: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in schema:
            definitions = REVIEW_OUTPUT_SCHEMA["$defs"]
            assert isinstance(definitions, dict)
            return definitions[schema["$ref"].removeprefix("#/$defs/")]
        return schema

    properties = REVIEW_OUTPUT_SCHEMA["properties"]
    assert isinstance(properties, dict)
    finding = resolve(properties["findings"]["items"])
    carried_forward = resolve(properties["carried_forward"]["items"])
    location = resolve(finding["properties"]["code_location"])
    line_range = resolve(location["properties"]["line_range"])
    for schema, expected_fields in [
        (REVIEW_OUTPUT_SCHEMA, set(review_payload())),
        (finding, set(finding_payload())),
        (carried_forward, {"comment_id", "current_evidence"}),
        (location, {"absolute_file_path", "line_range"}),
        (line_range, {"start", "end"}),
    ]:
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == expected_fields
        assert set(schema["required"]) == expected_fields
    assert line_range["properties"]["start"]["type"] == "integer"
    assert line_range["properties"]["end"]["type"] == "integer"


def test_resume_bootstrap_imports_without_installed_dependencies() -> None:
    subprocess.run(
        [sys.executable, "-S", "-c", "import cli.review.prepare_resume_state"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
