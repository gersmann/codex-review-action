from __future__ import annotations

from pathlib import Path

from cli.review.resume_state import (
    compute_review_cache_key,
    load_latest_thread_id,
    parse_reviewed_head_sha,
    render_review_summary_metadata,
)


def test_review_summary_metadata_round_trips() -> None:
    metadata = render_review_summary_metadata("deadbeef")

    assert parse_reviewed_head_sha(f"Codex Autonomous Review:\n{metadata}") == "deadbeef"
    assert parse_reviewed_head_sha("Codex Autonomous Review:\n<!-- broken -->") is None


def test_compute_review_cache_key_sanitizes_components() -> None:
    cache_key = compute_review_cache_key("owner/repo", 17, "gpt-5.4 turbo", "deadbeef")

    assert cache_key == "codex-review-v1-owner-repo-pr-17-gpt-5.4-turbo-deadbeef"


def test_load_latest_thread_id_uses_most_recent_updated_at(tmp_path: Path) -> None:
    session_index = tmp_path / "session_index.jsonl"
    session_index.write_text(
        "\n".join(
            [
                '{"id":"thread-1","thread_name":"Older","updated_at":"2026-03-27T10:00:00Z"}',
                '{"id":"thread-2","thread_name":"Newer","updated_at":"2026-03-27T11:00:00Z"}',
                '{"id":"thread-3","thread_name":"Broken"}',
                "not-json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert load_latest_thread_id(tmp_path) == "thread-2"
    assert load_latest_thread_id(tmp_path / "missing") is None
