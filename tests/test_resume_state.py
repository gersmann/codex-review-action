from __future__ import annotations

from pathlib import Path

from cli.review.resume_state import (
    build_review_resume_outputs,
    compute_review_cache_key,
    extract_current_head_sha,
    find_previous_reviewed_sha,
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


def test_extract_current_head_sha_and_find_previous_reviewed_sha() -> None:
    assert extract_current_head_sha({"pull_request": {"head": {"sha": " abc123 "}}}) == "abc123"
    assert extract_current_head_sha({}) == ""

    previous_reviewed_sha = find_previous_reviewed_sha(
        [
            {"body": "Codex Autonomous Review:\nwithout metadata"},
            {"body": f"Codex Autonomous Review:\n{render_review_summary_metadata('deadbeef')}"},
        ]
    )

    assert previous_reviewed_sha == "deadbeef"


def test_build_review_resume_outputs_uses_previous_sha_for_restore_key(tmp_path: Path) -> None:
    outputs = build_review_resume_outputs(
        repository="owner/repo",
        pr_number=17,
        model_name="gpt-5.4",
        runner_temp=str(tmp_path),
        current_head_sha="newsha",
        previous_reviewed_sha="oldsha",
    )

    assert outputs == {
        "codex_home": str((tmp_path / "codex-review-state").resolve()),
        "previous_reviewed_sha": "oldsha",
        "restore_key": "codex-review-v1-owner-repo-pr-17-gpt-5.4-oldsha",
        "current_cache_key": "codex-review-v1-owner-repo-pr-17-gpt-5.4-newsha",
    }


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
