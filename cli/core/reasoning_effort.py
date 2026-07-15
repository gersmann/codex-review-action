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
    try:
        print(normalize_reasoning_effort(value))
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
