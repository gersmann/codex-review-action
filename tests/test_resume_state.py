from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from cli.review.resume_state import (
    _list_stored_threads,
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


def test_load_latest_thread_id_uses_most_recent_updated_at(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "cli.review.resume_state._list_stored_threads",
        lambda *, codex_home, cwd: [
            SimpleNamespace(id="thread-1", updatedAt=100),
            SimpleNamespace(id="thread-2", updatedAt=200),
        ],
    )

    assert load_latest_thread_id(tmp_path, tmp_path) == "thread-2"


def test_load_latest_thread_id_returns_none_when_sdk_lookup_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "cli.review.resume_state._list_stored_threads",
        lambda *, codex_home, cwd: [],
    )

    assert load_latest_thread_id(tmp_path, tmp_path) is None


def test_list_stored_threads_uses_sdk_pagination(monkeypatch, tmp_path: Path) -> None:
    pages = [
        SimpleNamespace(
            data=[SimpleNamespace(id="thread-1", updatedAt=100)],
            next_cursor="cursor-2",
        ),
        SimpleNamespace(
            data=[SimpleNamespace(id="thread-2", updatedAt=200)],
            next_cursor=None,
        ),
    ]
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = (exc_type, exc, tb)

        def list_threads_page(self, options):
            calls = captured.setdefault("calls", [])
            assert isinstance(calls, list)
            calls.append(options)
            return pages[len(calls) - 1]

    def _fake_connect_stdio(process_options):
        captured["process_options"] = process_options
        return _FakeClient()

    monkeypatch.setattr(
        "cli.review.resume_state.AppServerClient.connect_stdio",
        _fake_connect_stdio,
    )

    threads = _list_stored_threads(codex_home=tmp_path / "codex-home", cwd=tmp_path)

    assert [thread.id for thread in threads] == ["thread-1", "thread-2"]
    process_options = cast(Any, captured["process_options"])
    assert process_options.env == {"CODEX_HOME": str(tmp_path / "codex-home")}
    calls = cast(list[Any], captured["calls"])
    assert [call.cwd for call in calls] == [str(tmp_path.resolve()), str(tmp_path.resolve())]
    assert [call.cursor for call in calls] == [None, "cursor-2"]


def test_list_stored_threads_returns_empty_on_sdk_error(monkeypatch, tmp_path: Path) -> None:
    def _fake_connect_stdio(process_options):
        _ = process_options
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "cli.review.resume_state.AppServerClient.connect_stdio",
        _fake_connect_stdio,
    )

    assert _list_stored_threads(codex_home=tmp_path / "codex-home", cwd=tmp_path) == []
