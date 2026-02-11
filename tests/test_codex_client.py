from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from codex.errors import CodexParseError

from cli.codex_client import CodexClient
from cli.config import ReviewConfig
from cli.exceptions import CodexExecutionError


@dataclass
class _RunCall:
    prompt: str


class _FakeThread:
    def __init__(self, events: Iterator[dict[str, Any]]) -> None:
        self._events = events
        self.calls: list[_RunCall] = []

    def run_streamed(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append(_RunCall(prompt=prompt))
        return SimpleNamespace(events=self._events)


class _FakeCodex:
    last_options: Any = None
    last_thread_options: Any = None
    thread: _FakeThread

    def __init__(self, options: Any) -> None:
        _FakeCodex.last_options = options

    def start_thread(self, options: Any) -> _FakeThread:
        _FakeCodex.last_thread_options = options
        return _FakeCodex.thread


def _make_config(*, debug_level: int = 0) -> ReviewConfig:
    return ReviewConfig.from_args(
        github_token="token",
        repository="o/r",
        stream_output=False,
        debug_level=debug_level,
    )


def test_execute_streams_agent_message_from_item_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeCodex.thread = _FakeThread(
        iter(
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {
                    "type": "item.updated",
                    "item": {"id": "m1", "type": "agent_message", "text": "Hel"},
                },
                {
                    "type": "item.updated",
                    "item": {"id": "m1", "type": "agent_message", "text": "Hello"},
                },
                {
                    "type": "item.completed",
                    "item": {"id": "m1", "type": "agent_message", "text": "Hello"},
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
                },
            ]
        )
    )
    monkeypatch.setattr("cli.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    output = client.execute("prompt", sandbox_mode="danger-full-access")

    assert output == "Hello"
    assert _FakeCodex.thread.calls[0].prompt == "prompt"
    assert _FakeCodex.last_thread_options.sandbox_mode == "danger-full-access"
    assert _FakeCodex.last_thread_options.model_reasoning_effort == "medium"
    assert _FakeCodex.last_options.config["show_raw_agent_reasoning"] is False


def test_execute_raises_on_turn_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeCodex.thread = _FakeThread(
        iter(
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.failed", "error": {"message": "boom"}},
            ]
        )
    )
    monkeypatch.setattr("cli.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    with pytest.raises(CodexExecutionError, match="boom"):
        client.execute("prompt")


class _ParseErrorIterator:
    def __iter__(self) -> _ParseErrorIterator:
        return self

    def __next__(self) -> dict[str, Any]:
        raise CodexParseError("bad event")


def test_execute_returns_empty_string_on_parse_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeCodex.thread = _FakeThread(_ParseErrorIterator())
    monkeypatch.setattr("cli.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    output = client.execute("prompt")

    assert output == ""


def test_execute_falls_back_to_medium_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeCodex.thread = _FakeThread(
        iter(
            [
                {
                    "type": "item.completed",
                    "item": {"id": "m1", "type": "agent_message", "text": "ok"},
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
                },
            ]
        )
    )
    monkeypatch.setattr("cli.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    output = client.execute("prompt", reasoning_effort="INVALID")

    assert output == "ok"
    assert _FakeCodex.last_thread_options.model_reasoning_effort == "medium"


def test_debug_level1_logs_turn_completed_summary(capsys: pytest.CaptureFixture[str]) -> None:
    client = CodexClient(_make_config(debug_level=1))
    client._emit_debug_event(
        None,
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 7, "cached_input_tokens": 2, "output_tokens": 3},
        },
    )

    err = capsys.readouterr().err
    assert "turn.completed usage in=7 cached=2 out=3" in err


def test_debug_level1_logs_non_agent_item_update_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=1))
    client._emit_debug_event(
        None,
        {
            "type": "item.updated",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "status": "in_progress",
                "command": "pytest -q",
            },
        },
    )

    err = capsys.readouterr().err
    assert "item.updated command_execution#cmd-1 status=in_progress command=pytest -q" in err


def test_debug_level1_suppresses_agent_message_updates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=1))
    client._emit_debug_event(
        None,
        {
            "type": "item.updated",
            "item": {"id": "msg-1", "type": "agent_message", "text": "hello"},
        },
    )

    err = capsys.readouterr().err
    assert err == ""
