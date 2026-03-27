from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from codex.errors import CodexParseError, ThreadRunError
from codex.protocol import types as protocol

from cli.clients.codex_client import CodexClient
from cli.core.config import ReviewConfig
from cli.core.exceptions import CodexExecutionError


@dataclass
class _RunCall:
    prompt: str
    turn_options: Any | None


class _FakeStream:
    def __init__(
        self,
        events: Iterator[object],
        *,
        final_text: str = "",
        wait_error: Exception | None = None,
    ) -> None:
        self._events = iter(events)
        self.final_text = final_text
        self.wait_error = wait_error

    def __iter__(self) -> _FakeStream:
        return self

    def __next__(self) -> object:
        return next(self._events)

    def wait(self) -> _FakeStream:
        if self.wait_error is not None:
            raise self.wait_error
        return self


class _FakeThread:
    def __init__(self, streams: list[_FakeStream]) -> None:
        self._streams = list(streams)
        self.calls: list[_RunCall] = []

    def run(self, prompt: str, *, turn_options: Any | None = None) -> _FakeStream:
        self.calls.append(_RunCall(prompt=prompt, turn_options=turn_options))
        if not self._streams:
            raise AssertionError("No fake stream queued")
        return self._streams.pop(0)


class _FakeCodex:
    last_options: Any = None
    last_thread_options: Any = None
    last_resume_options: Any = None
    last_resume_thread_id: str | None = None
    resume_error: Exception | None = None
    thread: _FakeThread

    def __init__(self, options: Any) -> None:
        _FakeCodex.last_options = options

    def start_thread(self, options: Any) -> _FakeThread:
        _FakeCodex.last_thread_options = options
        return _FakeCodex.thread

    def resume_thread(self, thread_id: str, options: Any) -> _FakeThread:
        _FakeCodex.last_resume_thread_id = thread_id
        _FakeCodex.last_resume_options = options
        if _FakeCodex.resume_error is not None:
            raise _FakeCodex.resume_error
        return _FakeCodex.thread


def _make_config(*, debug_level: int = 0, stream_output: bool = False) -> ReviewConfig:
    return ReviewConfig.from_args(
        github_token="token",
        repository="o/r",
        pr_number=1,
        openai_api_key="test-key",
        stream_output=stream_output,
        debug_level=debug_level,
    )


def _reset_fake_codex() -> None:
    _FakeCodex.last_options = None
    _FakeCodex.last_thread_options = None
    _FakeCodex.last_resume_options = None
    _FakeCodex.last_resume_thread_id = None
    _FakeCodex.resume_error = None


def _agent_message_delta(delta: str) -> protocol.ItemAgentMessageDeltaNotification:
    return protocol.ItemAgentMessageDeltaNotification.model_validate(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "delta": delta,
                "itemId": "m1",
                "threadId": "thread-1",
                "turnId": "turn-1",
            },
        }
    )


def _agent_message_completed(
    text: str,
    *,
    phase: str | None = None,
) -> protocol.ItemCompletedNotificationModel:
    item: dict[str, object] = {
        "id": "m1",
        "text": text,
        "type": "agentMessage",
    }
    if phase is not None:
        item["phase"] = phase
    return protocol.ItemCompletedNotificationModel.model_validate(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": item,
            },
        }
    )


def _turn_completed(status: str = "completed", message: str | None = None) -> Any:
    turn_payload: dict[str, object] = {"id": "turn-1", "items": [], "status": status}
    payload: dict[str, object] = {
        "method": "turn/completed",
        "params": {"threadId": "thread-1", "turn": turn_payload},
    }
    if message is not None:
        turn_payload["error"] = {"message": message}
    return protocol.TurnCompletedNotificationModel.model_validate(payload)


def _reasoning_item_completed(
    *,
    content: list[str] | None = None,
    summary: list[str] | None = None,
) -> protocol.ItemCompletedNotificationModel:
    return protocol.ItemCompletedNotificationModel.model_validate(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "r1",
                    "type": "reasoning",
                    "content": content or [],
                    "summary": summary or [],
                },
            },
        }
    )


def _reasoning_text_delta(delta: str) -> protocol.ItemReasoningTextDeltaNotification:
    return protocol.ItemReasoningTextDeltaNotification.model_validate(
        {
            "method": "item/reasoning/textDelta",
            "params": {
                "delta": delta,
                "itemId": "r1",
                "contentIndex": 0,
                "threadId": "thread-1",
                "turnId": "turn-1",
            },
        }
    )


def _reasoning_summary_text_delta(
    delta: str,
) -> protocol.ItemReasoningSummaryTextDeltaNotification:
    return protocol.ItemReasoningSummaryTextDeltaNotification.model_validate(
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {
                "delta": delta,
                "itemId": "r1",
                "summaryIndex": 0,
                "threadId": "thread-1",
                "turnId": "turn-1",
            },
        }
    )


def _command_item_started(
    item_id: str,
    command: str,
) -> protocol.ItemStartedNotificationModel:
    return protocol.ItemStartedNotificationModel.model_validate(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": item_id,
                    "type": "commandExecution",
                    "command": command,
                    "commandActions": [],
                    "cwd": "/tmp",
                    "status": "inProgress",
                },
            },
        }
    )


def _command_item_completed(
    item_id: str,
    command: str,
) -> protocol.ItemCompletedNotificationModel:
    return protocol.ItemCompletedNotificationModel.model_validate(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": item_id,
                    "type": "commandExecution",
                    "command": command,
                    "commandActions": [],
                    "cwd": "/tmp",
                    "status": "completed",
                    "exitCode": 0,
                    "durationMs": 12,
                    "aggregatedOutput": "ok",
                },
            },
        }
    )


def _command_terminal_interaction(
    item_id: str,
    stdin: str,
) -> protocol.ItemCommandExecutionTerminalInteractionNotification:
    return protocol.ItemCommandExecutionTerminalInteractionNotification.model_validate(
        {
            "method": "item/commandExecution/terminalInteraction",
            "params": {
                "itemId": item_id,
                "processId": "pid-1",
                "stdin": stdin,
                "threadId": "thread-1",
                "turnId": "turn-1",
            },
        }
    )


def _root_value(value: object) -> object:
    root = getattr(value, "root", None)
    return root if root is not None else value


def test_execute_text_streams_agent_message_from_protocol_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread(
        [
            _FakeStream(
                iter(
                    [
                        _agent_message_delta("Hel"),
                        _agent_message_delta("lo"),
                        _turn_completed(),
                    ]
                ),
                final_text="Hello",
            )
        ]
    )
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    output = client.execute_text("prompt", sandbox_mode="danger-full-access")

    assert output == "Hello"
    assert _FakeCodex.thread.calls[0].prompt == "prompt"
    assert _root_value(_FakeCodex.last_thread_options.sandbox) == "danger-full-access"
    assert _FakeCodex.last_thread_options.config.web_search == "live"
    turn_options = _FakeCodex.thread.calls[0].turn_options
    assert turn_options is not None
    assert _root_value(turn_options.effort) == "medium"
    assert _FakeCodex.last_options.config.show_raw_agent_reasoning is False


def test_execute_text_does_not_duplicate_streamed_output_when_completion_arrives(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread(
        [
            _FakeStream(
                iter(
                    [
                        _agent_message_delta("Hel"),
                        _agent_message_delta("lo"),
                        _agent_message_completed("Hello"),
                        _turn_completed(),
                    ]
                ),
                final_text="",
            )
        ]
    )
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config(stream_output=True))

    output = client.execute_text("prompt")

    streamed = capsys.readouterr().out
    assert output == "Hello"
    assert streamed == "Hello\n"


def test_execute_text_enables_raw_reasoning_at_debug2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread([_FakeStream(iter([_turn_completed()]), final_text="ok")])
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config(debug_level=2))

    output = client.execute_text("prompt")

    assert output == "ok"
    assert _FakeCodex.last_options.config.show_raw_agent_reasoning is True


def test_execute_text_raises_on_thread_run_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread([_FakeStream(iter(()), wait_error=ThreadRunError("boom"))])
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    with pytest.raises(CodexExecutionError, match="boom"):
        client.execute_text("prompt")


def test_execute_text_raises_on_failed_turn_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread([_FakeStream(iter([_turn_completed("failed", "bad")]))])
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    with pytest.raises(CodexExecutionError, match="Codex error: bad"):
        client.execute_text("prompt")


def test_execute_text_raises_on_interrupted_turn_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread([_FakeStream(iter([_turn_completed("interrupted")]))])
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    with pytest.raises(CodexExecutionError, match="Codex error: turn interrupted"):
        client.execute_text("prompt")


class _ParseErrorIterator:
    def __iter__(self) -> _ParseErrorIterator:
        return self

    def __next__(self) -> object:
        raise CodexParseError("bad event")


def test_execute_text_raises_on_parse_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread([_FakeStream(_ParseErrorIterator())])
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    with pytest.raises(CodexExecutionError, match="stream parsing failed"):
        client.execute_text("prompt")


def test_execute_structured_runs_second_turn_with_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    _FakeCodex.thread = _FakeThread(
        [
            _FakeStream(iter([_turn_completed()]), final_text="Intermediate"),
            _FakeStream(iter([_turn_completed()]), final_text='{"summary":"ok"}'),
        ]
    )
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    output = client.execute_structured(
        "prompt",
        output_schema=schema,
        schema_prompt="Return the JSON now.",
    )

    assert output == '{"summary":"ok"}'
    assert len(_FakeCodex.thread.calls) == 2
    assert _FakeCodex.thread.calls[0].prompt == "prompt"
    first_turn_options = _FakeCodex.thread.calls[0].turn_options
    second_turn_options = _FakeCodex.thread.calls[1].turn_options
    assert first_turn_options is not None
    assert second_turn_options is not None
    assert _root_value(first_turn_options.effort) == "medium"
    assert _FakeCodex.thread.calls[1].prompt == "Return the JSON now."
    assert _root_value(second_turn_options.effort) == "medium"
    assert second_turn_options.output_schema == schema


def test_execute_structured_raises_when_schema_turn_emits_no_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    _FakeCodex.thread = _FakeThread(
        [
            _FakeStream(iter([_turn_completed()]), final_text="Intermediate prose"),
            _FakeStream(iter([_turn_completed()])),
        ]
    )
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    with pytest.raises(
        CodexExecutionError,
        match="Codex did not return structured output on turn 2.",
    ):
        client.execute_structured(
            "prompt",
            output_schema=schema,
            schema_prompt="Return the JSON now.",
        )


def test_execute_text_falls_back_to_medium_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread([_FakeStream(iter([_turn_completed()]), final_text="ok")])
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    output = client.execute_text("prompt", reasoning_effort="INVALID")

    assert output == "ok"
    turn_options = _FakeCodex.thread.calls[0].turn_options
    assert turn_options is not None
    assert _root_value(turn_options.effort) == "medium"


def test_execute_text_uses_config_api_key_not_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    _FakeCodex.thread = _FakeThread([_FakeStream(iter([_turn_completed()]), final_text="ok")])
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    config = ReviewConfig.from_args(
        github_token="token",
        repository="o/r",
        pr_number=1,
        openai_api_key="config-key",
    )
    client = CodexClient(config)

    output = client.execute_text("prompt")

    assert output == "ok"
    assert _FakeCodex.last_options.api_key == "config-key"


def test_execute_structured_resumes_existing_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _reset_fake_codex()
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    _FakeCodex.thread = _FakeThread(
        [
            _FakeStream(iter([_turn_completed()]), final_text="Intermediate"),
            _FakeStream(iter([_turn_completed()]), final_text='{"summary":"ok"}'),
        ]
    )
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    client = CodexClient(_make_config())

    output = client.execute_structured(
        "prompt",
        output_schema=schema,
        resume_thread_id="thread-123",
    )

    assert output == '{"summary":"ok"}'
    assert _FakeCodex.last_resume_thread_id == "thread-123"
    assert _FakeCodex.last_resume_options is not None
    assert _FakeCodex.last_resume_options.persist_extended_history is True
    assert _FakeCodex.last_options.env == {"CODEX_HOME": os.environ["CODEX_HOME"]}


def test_execute_structured_falls_back_to_fresh_thread_when_resume_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_codex()
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    _FakeCodex.thread = _FakeThread(
        [
            _FakeStream(iter([_turn_completed()]), final_text="Intermediate"),
            _FakeStream(iter([_turn_completed()]), final_text='{"summary":"ok"}'),
        ]
    )
    _FakeCodex.resume_error = RuntimeError("missing cached thread")
    monkeypatch.setattr("cli.clients.codex_client.Codex", _FakeCodex)
    client = CodexClient(_make_config())

    output = client.execute_structured(
        "prompt",
        output_schema=schema,
        resume_thread_id="thread-123",
    )

    assert output == '{"summary":"ok"}'
    assert _FakeCodex.last_resume_thread_id == "thread-123"
    assert _FakeCodex.last_thread_options is not None
    assert _FakeCodex.last_thread_options.persist_extended_history is True


def test_debug_level1_logs_token_usage_update_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=1))
    client._emit_debug_event(
        protocol.ThreadTokenUsageUpdatedNotificationModel.model_validate(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "tokenUsage": {
                        "last": {
                            "inputTokens": 7,
                            "cachedInputTokens": 2,
                            "outputTokens": 3,
                            "reasoningOutputTokens": 1,
                            "totalTokens": 10,
                        },
                        "total": {
                            "inputTokens": 7,
                            "cachedInputTokens": 2,
                            "outputTokens": 3,
                            "reasoningOutputTokens": 1,
                            "totalTokens": 10,
                        },
                        "modelContextWindow": 1000,
                    },
                },
            }
        )
    )

    err = capsys.readouterr().err
    assert "thread.token_usage usage in=7 cached=2 out=3" in err


def test_debug_level1_logs_command_started_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=1))
    client._emit_debug_event(_command_item_started("cmd-1", "pytest -q"))

    err = capsys.readouterr().err
    assert "item.started command_execution#cmd-1 status=inProgress command=pytest -q" in err


def test_debug_level1_logs_command_completed_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=1))
    client._emit_debug_event(_command_item_completed("cmd-1", "pytest -q"))

    err = capsys.readouterr().err
    assert "item.completed command_execution#cmd-1 status=completed" in err
    assert "exit_code=0" in err
    assert "duration_ms=12" in err
    assert "output_chars=2" in err


def test_debug_level1_logs_reasoning_summary_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=1))
    client._emit_debug_event(_reasoning_item_completed(summary=["Inspecting the diff first."]))

    err = capsys.readouterr().err
    assert "item.completed reasoning#r1 summary=Inspecting the diff first." in err


def test_debug_level2_suppresses_reasoning_summary_delta_spam(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=2))
    client._emit_debug_event(_reasoning_summary_text_delta("noise"))

    err = capsys.readouterr().err
    assert err == ""


def test_debug_level2_logs_reasoning_text_deltas_concisely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=2))
    client._emit_debug_event(_reasoning_text_delta("Inspecting diff"))

    err = capsys.readouterr().err
    assert "[codex-reasoning] Inspecting diff" in err


def test_debug_level2_suppresses_agent_message_deltas(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=2))
    client._emit_debug_event(_agent_message_delta("hello"))

    err = capsys.readouterr().err
    assert err == ""


def test_debug_level1_logs_terminal_interaction_concisely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = CodexClient(_make_config(debug_level=1))
    client._emit_debug_event(_command_terminal_interaction("cmd-1", "q"))

    err = capsys.readouterr().err
    assert "command_execution#cmd-1 terminal_input process=pid-1 stdin=q" in err


def test_debug_level2_logs_completed_agent_message_text_concisely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    long_text = "x" * 260
    client = CodexClient(_make_config(debug_level=2))
    client._emit_debug_event(_agent_message_completed(long_text, phase="final_answer"))

    err = capsys.readouterr().err
    assert "[codex-agent-message] item.completed id=m1 phase=final_answer chars=260 text=" in err
    assert "chars=260" in err
    assert long_text not in err
    assert "..." not in err
    assert "…" in err


def test_debug_level2_still_truncates_non_agent_message_payloads(
    capsys: pytest.CaptureFixture[str],
) -> None:
    long_text = "x" * 260
    client = CodexClient(_make_config(debug_level=2))
    client._emit_debug_event(_command_item_completed("cmd-1", long_text))

    err = capsys.readouterr().err
    assert "ItemCompletedNotificationModel" in err
    assert long_text not in err
    assert ("x" * 120) in err
    assert "…" in err
