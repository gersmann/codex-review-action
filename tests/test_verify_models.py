from __future__ import annotations

import pytest

from cli.core.exceptions import ReviewContractError
from cli.core.models import VerifyRunResult


def test_verify_result_parses_valid_payload() -> None:
    result = VerifyRunResult.from_payload(
        {
            "verdict": "incorrect",
            "explanation": "The guard already handles None.",
            "confidence_score": 0.8,
        }
    )

    assert result.verdict == "incorrect"
    assert result.explanation == "The guard already handles None."
    assert result.confidence_score == 0.8


def test_verify_result_rejects_unknown_verdict() -> None:
    with pytest.raises(ReviewContractError):
        VerifyRunResult.from_payload(
            {"verdict": "maybe", "explanation": "x", "confidence_score": None}
        )


def test_verify_result_requires_fields() -> None:
    with pytest.raises(ReviewContractError):
        VerifyRunResult.from_payload({"verdict": "correct"})
