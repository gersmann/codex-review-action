from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from codex.protocol import types as protocol
from pydantic import BaseModel

_TRACE_MAX_STRING_CHARS = 200
_TRACE_MAX_LIST_ITEMS = 8


@dataclass(frozen=True)
class _DebugItemContext:
    item: object
    event_name: str


class CodexEventDebugger:
    """Formats and emits debug output for codex protocol events."""

    def __init__(self, *, debug_level: int, debug_fn: Callable[[int, str], None]) -> None:
        self._debug_level = debug_level
        self._debug_fn = debug_fn

    def emit(self, event: BaseModel) -> None:
        if self._debug_level < 1:
            return

        summary = self._summarize_protocol_event_for_debug1(event)
        if summary:
            self._debug_fn(1, f"[codex-event] {summary}")

        if self._debug_level < 2:
            return

        trace = self._format_debug2_event(event)
        if trace:
            self._debug_fn(2, trace)

    def _summarize_protocol_event_for_debug1(self, event: BaseModel) -> str | None:
        if isinstance(event, protocol.ThreadStartedNotificationModel):
            return f"thread.started thread_id={event.params.thread.id}"

        if isinstance(event, protocol.TurnStartedNotificationModel):
            return "turn.started"

        if isinstance(event, protocol.ThreadTokenUsageUpdatedNotificationModel):
            usage = event.params.tokenUsage.last
            return (
                "thread.token_usage "
                f"usage in={usage.inputTokens} cached={usage.cachedInputTokens} "
                f"out={usage.outputTokens}"
            )

        if isinstance(event, protocol.TurnCompletedNotificationModel):
            return self._summarize_turn_completion_for_debug1(event)

        if isinstance(event, protocol.ErrorNotificationModel):
            return f"error message={self._clip(event.params.error.message)}"

        item_event_summary = self._summarize_item_lifecycle_for_debug1(event)
        if item_event_summary is not None:
            return item_event_summary

        if isinstance(event, protocol.ItemCommandExecutionTerminalInteractionNotification):
            return (
                "command_execution#"
                f"{event.params.itemId} terminal_input process={event.params.processId} "
                f"stdin={self._clip(event.params.stdin, 80)}"
            )

        return None

    def _summarize_turn_completion_for_debug1(
        self,
        event: protocol.TurnCompletedNotificationModel,
    ) -> str | None:
        status = event.params.turn.status.root
        if status == "failed":
            error = event.params.turn.error
            if error is not None and error.message.strip():
                return f"turn.failed message={self._clip(error.message)}"
            return "turn.failed"
        if status == "interrupted":
            return "turn.failed message=Turn interrupted"
        return None

    def _summarize_item_lifecycle_for_debug1(self, event: BaseModel) -> str | None:
        if isinstance(event, protocol.ItemStartedNotificationModel):
            return self._summarize_item_for_debug1(event.params.item.root, "item.started")
        if isinstance(event, protocol.ItemCompletedNotificationModel):
            return self._summarize_item_for_debug1(event.params.item.root, "item.completed")
        return None

    def _summarize_item_for_debug1(self, item: object, event_name: str) -> str | None:
        context = _DebugItemContext(item=item, event_name=event_name)
        for handler in (
            self._summarize_agent_message_item_for_debug1,
            self._summarize_command_execution_item_for_debug1,
            self._summarize_file_change_item_for_debug1,
            self._summarize_mcp_tool_call_item_for_debug1,
            self._summarize_web_search_item_for_debug1,
            self._summarize_reasoning_item_for_debug1,
            self._summarize_generic_model_item_for_debug1,
        ):
            summary = handler(context)
            if summary is not None:
                return summary
        return None

    def _summarize_agent_message_item_for_debug1(self, context: _DebugItemContext) -> str | None:
        if not isinstance(context.item, protocol.AgentMessageThreadItem):
            return None
        if context.event_name != "item.completed":
            return None
        return (
            f"{context.event_name} agent_message#{context.item.id} chars={len(context.item.text)}"
        )

    def _summarize_command_execution_item_for_debug1(
        self,
        context: _DebugItemContext,
    ) -> str | None:
        if not isinstance(context.item, protocol.CommandExecutionThreadItem):
            return None
        summary = (
            f"{context.event_name} command_execution#{context.item.id} "
            f"status={context.item.status.root} "
            f"command={self._clip(context.item.command)}"
        )
        if context.item.exitCode is not None:
            summary += f" exit_code={context.item.exitCode}"
        if context.item.durationMs is not None:
            summary += f" duration_ms={context.item.durationMs}"
        if context.item.aggregatedOutput is not None:
            summary += f" output_chars={len(context.item.aggregatedOutput)}"
        return summary

    def _summarize_file_change_item_for_debug1(self, context: _DebugItemContext) -> str | None:
        if not isinstance(context.item, protocol.FileChangeThreadItem):
            return None
        return (
            f"{context.event_name} file_change#{context.item.id} "
            f"status={context.item.status.root} changes={len(context.item.changes)}"
        )

    def _summarize_mcp_tool_call_item_for_debug1(self, context: _DebugItemContext) -> str | None:
        if not isinstance(context.item, protocol.McpToolCallThreadItem):
            return None
        return (
            f"{context.event_name} mcp_tool_call#{context.item.id} "
            f"status={context.item.status.root} "
            f"server={context.item.server} tool={context.item.tool}"
        )

    def _summarize_web_search_item_for_debug1(self, context: _DebugItemContext) -> str | None:
        if not isinstance(context.item, protocol.WebSearchThreadItem):
            return None
        query = self._clip(context.item.query)
        if not query:
            return None
        return f"{context.event_name} web_search#{context.item.id} query={query}"

    def _summarize_reasoning_item_for_debug1(self, context: _DebugItemContext) -> str | None:
        if not isinstance(context.item, protocol.ReasoningThreadItem):
            return None
        if context.event_name != "item.completed":
            return None
        content_text = self._join_text_parts(context.item.content)
        summary_text = self._join_text_parts(context.item.summary)
        reasoning_kind = "raw" if content_text else "summary"
        reasoning_text = content_text or summary_text
        if reasoning_text:
            return (
                f"{context.event_name} reasoning#{context.item.id} "
                f"{reasoning_kind}={self._clip(reasoning_text, 200)}"
            )
        return f"{context.event_name} reasoning#{context.item.id}"

    def _summarize_generic_model_item_for_debug1(self, context: _DebugItemContext) -> str | None:
        if not isinstance(context.item, BaseModel):
            return None
        payload = context.item.model_dump(mode="python", by_alias=True, exclude_none=True)
        item_id_obj = payload.get("id")
        item_id = item_id_obj if isinstance(item_id_obj, str) and item_id_obj else "unknown"
        item_type = self._camel_to_snake(type(context.item).__name__.removesuffix("ThreadItem"))
        return f"{context.event_name} {item_type}#{item_id}"

    def _format_debug2_event(self, event: BaseModel) -> str | None:
        if isinstance(
            event,
            (
                protocol.ItemReasoningSummaryPartAddedNotification,
                protocol.ItemReasoningSummaryTextDeltaNotification,
                protocol.ItemAgentMessageDeltaNotification,
            ),
        ):
            return None

        if isinstance(event, protocol.ItemReasoningTextDeltaNotification):
            delta = event.params.delta
            if delta.strip():
                return f"[codex-reasoning] {self._clip(delta, 200)}"
            return None

        if isinstance(event, protocol.ItemCommandExecutionTerminalInteractionNotification):
            return (
                "[codex-command] "
                f"terminal_input item#{event.params.itemId} process={event.params.processId} "
                f"stdin={self._clip(event.params.stdin, 80)}"
            )

        if isinstance(event, protocol.ItemCompletedNotificationModel):
            item = event.params.item.root
            if isinstance(item, protocol.AgentMessageThreadItem):
                phase = item.phase.root if item.phase is not None else "unspecified"
                text = item.text
                return (
                    "[codex-agent-message] "
                    f"item.completed id={item.id} phase={phase} chars={len(text)} "
                    f"text={self._clip(text, 200)}"
                )

        payload = self._truncate_payload(
            event.model_dump(mode="python", by_alias=True, exclude_none=True)
        )
        return f"[codex-event] {type(event).__name__}: {payload}"

    def _truncate_payload(self, value: object) -> object:
        if isinstance(value, str):
            return self._clip(value, _TRACE_MAX_STRING_CHARS)
        if isinstance(value, list):
            items = [self._truncate_payload(item) for item in value[:_TRACE_MAX_LIST_ITEMS]]
            if len(value) > _TRACE_MAX_LIST_ITEMS:
                items.append(f"... {len(value) - _TRACE_MAX_LIST_ITEMS} more")
            return items
        if isinstance(value, dict):
            return {key: self._truncate_payload(item) for key, item in value.items()}
        return value

    def _join_text_parts(self, parts: list[str] | None) -> str:
        return " ".join(part for part in parts or [] if isinstance(part, str)).strip()

    def _clip(self, text: str, max_chars: int = 120) -> str:
        stripped = " ".join(text.split())
        if len(stripped) <= max_chars:
            return stripped
        return stripped[: max_chars - 1] + "…"

    def _camel_to_snake(self, name: str) -> str:
        chars: list[str] = []
        for index, char in enumerate(name):
            if char.isupper() and index > 0:
                chars.append("_")
            chars.append(char.lower())
        return "".join(chars)
