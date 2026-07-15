from __future__ import annotations

import os
import subprocess
import sys

import pytest
from codex import TurnOptions
from codex.protocol import types as protocol

from cli.core.reasoning_effort import (
    REASONING_EFFORT_VALUES,
    default_reasoning_effort_for_model,
    normalize_reasoning_effort,
)
from cli.main import create_parser


def test_reasoning_effort_values_match_verified_openai_model_values() -> None:
    assert REASONING_EFFORT_VALUES == (
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )


@pytest.mark.parametrize("value", REASONING_EFFORT_VALUES)
def test_reasoning_effort_values_round_trip_through_sdk_turn_options(value: str) -> None:
    options = TurnOptions(effort=protocol.ReasoningEffort(value))

    assert options.effort is not None
    assert options.effort.root == value


@pytest.mark.parametrize("value", ["xhigh", "x-high", "X_HIGH", " XHigh "])
def test_normalize_reasoning_effort_accepts_xhigh_aliases(value: str) -> None:
    assert normalize_reasoning_effort(value) == "xhigh"


def test_normalize_reasoning_effort_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="Invalid reasoning effort 'extreme'"):
        normalize_reasoning_effort("extreme")


@pytest.mark.parametrize(
    ("model_name", "expected_effort"),
    [
        ("gpt-5.6-luna", "xhigh"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "medium"),
        ("GPT-5.6-LUNA", "xhigh"),
        ("gpt-9-unknown", "high"),
        ("", "high"),
    ],
)
def test_default_reasoning_effort_for_model(model_name: str, expected_effort: str) -> None:
    assert default_reasoning_effort_for_model(model_name) == expected_effort


def test_parser_normalizes_reasoning_effort_alias() -> None:
    args = create_parser().parse_args(["--reasoning-effort", "x-high"])

    assert args.reasoning_effort == "xhigh"


def test_validation_module_reads_action_input_from_environment() -> None:
    env = os.environ | {"CODEX_REASONING_EFFORT_INPUT": "X_HIGH"}

    result = subprocess.run(
        [sys.executable, "-m", "cli.core.reasoning_effort"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "xhigh"


def test_validation_module_accepts_empty_action_input_as_unset() -> None:
    env = os.environ | {"CODEX_REASONING_EFFORT_INPUT": ""}

    result = subprocess.run(
        [sys.executable, "-m", "cli.core.reasoning_effort"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_validation_module_exits_two_for_unknown_action_input() -> None:
    env = os.environ | {"CODEX_REASONING_EFFORT_INPUT": "extreme"}

    result = subprocess.run(
        [sys.executable, "-m", "cli.core.reasoning_effort"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 2
    assert "Invalid reasoning effort 'extreme'" in result.stderr
