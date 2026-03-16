from __future__ import annotations

from codex.protocol import types as protocol

from cli.clients.codex_event_debugger import CodexEventDebugger


def test_event_debugger_emits_agent_message_completion_summary() -> None:
    messages: list[tuple[int, str]] = []
    debugger = CodexEventDebugger(
        debug_level=1, debug_fn=lambda level, msg: messages.append((level, msg))
    )

    event = protocol.ItemCompletedNotificationModel.model_validate(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "m1", "type": "agentMessage", "text": "ok"},
            },
        }
    )
    debugger.emit(event)

    assert messages == [(1, "[codex-event] item.completed agent_message#m1 chars=2")]


def test_event_debugger_suppresses_reasoning_summary_delta_noise() -> None:
    messages: list[tuple[int, str]] = []
    debugger = CodexEventDebugger(
        debug_level=2, debug_fn=lambda level, msg: messages.append((level, msg))
    )

    event = protocol.ItemReasoningSummaryTextDeltaNotification.model_validate(
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {
                "delta": " chunk",
                "itemId": "r1",
                "summaryIndex": 0,
                "threadId": "thread-1",
                "turnId": "turn-1",
            },
        }
    )
    debugger.emit(event)

    assert messages == []


def test_event_debugger_emits_reasoning_text_delta_at_debug2() -> None:
    messages: list[tuple[int, str]] = []
    debugger = CodexEventDebugger(
        debug_level=2, debug_fn=lambda level, msg: messages.append((level, msg))
    )

    event = protocol.ItemReasoningTextDeltaNotification.model_validate(
        {
            "method": "item/reasoning/textDelta",
            "params": {
                "delta": "Think step",
                "itemId": "r1",
                "contentIndex": 0,
                "threadId": "thread-1",
                "turnId": "turn-1",
            },
        }
    )
    debugger.emit(event)

    assert messages == [(2, "[codex-reasoning] Think step")]


def test_event_debugger_dedupes_repeated_token_usage_summary() -> None:
    messages: list[tuple[int, str]] = []
    debugger = CodexEventDebugger(
        debug_level=1, debug_fn=lambda level, msg: messages.append((level, msg))
    )

    event = protocol.ThreadTokenUsageUpdatedNotificationModel.model_validate(
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

    debugger.emit(event)
    debugger.emit(event)

    assert messages == [(1, "[codex-event] thread.token_usage usage in=7 cached=2 out=3")]
