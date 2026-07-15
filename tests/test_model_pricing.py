import subprocess
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

from cli.core.model_pricing import (
    MODEL_PRICING,
    SUPPORTED_REVIEW_MODELS,
    estimate_review_cost,
)
from cli.core.review_usage import ReviewUsage

ROOT = Path(__file__).resolve().parents[1]


def test_config_import_does_not_require_codex_sdk() -> None:
    script = textwrap.dedent(
        """
        import builtins

        original_import = builtins.__import__

        def import_without_codex(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "codex" or name.startswith("codex."):
                raise ModuleNotFoundError("blocked codex import")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = import_without_codex

        import cli.core.config
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_supported_review_models_have_verified_pricing() -> None:
    assert SUPPORTED_REVIEW_MODELS == (
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    )
    assert MODEL_PRICING["gpt-5.6-luna"].input_per_million == Decimal("1.00")
    assert MODEL_PRICING["gpt-5.6-luna"].cached_input_per_million == Decimal("0.10")
    assert MODEL_PRICING["gpt-5.6-luna"].output_per_million == Decimal("6.00")
    assert MODEL_PRICING["gpt-5.6-terra"].input_per_million == Decimal("2.50")
    assert MODEL_PRICING["gpt-5.6-terra"].cached_input_per_million == Decimal("0.25")
    assert MODEL_PRICING["gpt-5.6-terra"].output_per_million == Decimal("15.00")
    assert MODEL_PRICING["gpt-5.6-sol"].input_per_million == Decimal("5.00")
    assert MODEL_PRICING["gpt-5.6-sol"].cached_input_per_million == Decimal("0.50")
    assert MODEL_PRICING["gpt-5.6-sol"].output_per_million == Decimal("30.00")


def test_estimated_cost_separates_cached_input_and_does_not_double_charge_reasoning() -> None:
    usage = ReviewUsage(
        response_count=2,
        input_tokens=1_000_000,
        cached_input_tokens=200_000,
        output_tokens=500_000,
        reasoning_output_tokens=300_000,
        total_tokens=1_500_000,
    )

    assert estimate_review_cost("gpt-5.6-luna", usage) == Decimal("3.820")
