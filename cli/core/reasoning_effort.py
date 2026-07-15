from __future__ import annotations

import os
import sys

REASONING_EFFORT_VALUES = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def default_reasoning_effort_for_model(model_name: str) -> str:
    """Return the default reasoning effort for a model when none is specified."""
    name = model_name.lower()
    if "luna" in name:
        return "xhigh"
    if "terra" in name or "sol" in name:
        return "medium"
    return "high"


def normalize_reasoning_effort(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", "-")
        if normalized == "x-high":
            normalized = "xhigh"
        if normalized in REASONING_EFFORT_VALUES:
            return normalized

    allowed = "|".join(REASONING_EFFORT_VALUES)
    raise ValueError(f"Invalid reasoning effort '{value}' (allowed: {allowed})")


def main() -> int:
    value = os.environ.get("CODEX_REASONING_EFFORT_INPUT", "")
    if not value.strip():
        # Empty means "not specified": the model-dependent default applies later.
        return 0
    try:
        print(normalize_reasoning_effort(value))
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
