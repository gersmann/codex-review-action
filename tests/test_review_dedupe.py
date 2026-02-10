from __future__ import annotations

from cli.codex_client import CodexClient
from cli.config import ReviewConfig
from cli.review.dedupe import deduplicate_findings


def _make_config() -> ReviewConfig:
    return ReviewConfig.from_args(
        github_token="token",
        repository="o/r",
        stream_output=False,
    )


def test_semantic_dedupe_uses_robust_json_parser() -> None:
    client = CodexClient(_make_config())

    findings = [
        {
            "title": "A",
            "body": "Body",
            "code_location": {
                "absolute_file_path": "/tmp/a.py",
                "line_range": {"start": 10, "end": 10},
            },
        }
    ]

    filtered = deduplicate_findings(
        findings,
        existing_comments=[],
        execute_codex=lambda *args, **kwargs: '```json\n{"keep": [0]}\n```',
        parse_json_response=client.parse_json_response,
        fast_model_name="gpt-5-mini",
        fast_reasoning_effort="low",
    )

    assert filtered == findings


def test_semantic_dedupe_returns_original_on_bad_output() -> None:
    findings = [
        {
            "title": "A",
            "body": "Body",
            "code_location": {
                "absolute_file_path": "/tmp/a.py",
                "line_range": {"start": 10, "end": 10},
            },
        }
    ]

    filtered = deduplicate_findings(
        findings,
        existing_comments=[],
        execute_codex=lambda *args, **kwargs: "not json",
        parse_json_response=lambda raw: {"keep": "bad"} if raw else {},
        fast_model_name="gpt-5-mini",
        fast_reasoning_effort="low",
    )

    assert filtered == findings
