from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any, cast

from codex import Codex, CodexOptions, ThreadOptions, TurnOptions
from codex.errors import CodexParseError, ThreadRunError

from .config import ReviewConfig, make_debug
from .exceptions import CodexExecutionError

_REASONING_EFFORT_VALUES = {"minimal", "low", "medium", "high", "xhigh"}
_SANDBOX_MODE_VALUES = {"read-only", "workspace-write", "danger-full-access"}


def _as_int_or_none(value: object) -> int | None:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


class CodexClient:
    """Client for executing Codex with streaming and response parsing."""

    def __init__(self, config: ReviewConfig) -> None:
        self.config = config
        self._debug = make_debug(config)

    # -------- Internal helpers (control flow + I/O) --------
    def _should_stream(self, suppress_stream: bool) -> bool:
        """Return True if stdout streaming is enabled for this call."""
        return bool(self.config.stream_output and not suppress_stream)

    def _emit_debug_event(self, msg_type: str | None, msg: object) -> None:
        if self.config.debug_level < 1:
            return
        if isinstance(msg, Mapping):
            event_type_obj = msg.get("type")
            event_type = (
                str(event_type_obj) if isinstance(event_type_obj, str) else (msg_type or "")
            )

            summary = self._summarize_event_for_debug1(event_type, msg)
            if summary:
                self._debug(1, f"[codex-event] {summary}")
            elif event_type in {"error", "turn.failed"}:
                self._debug(1, f"[codex-event] {event_type}: {dict(msg)}")

            if self.config.debug_level >= 2:
                item_obj = msg.get("item")
                if event_type == "item.updated" and isinstance(item_obj, Mapping):
                    item_type_obj = item_obj.get("type")
                    if item_type_obj == "agent_message":
                        return
                self._debug(2, f"[codex-event] {event_type}: {dict(msg)}")
            return

        self._debug(1, f"[codex-event] {msg_type}: {msg}")

    def _summarize_event_for_debug1(self, event_type: str, msg: Mapping[str, object]) -> str | None:
        if event_type == "thread.started":
            thread_id_obj = msg.get("thread_id")
            thread_id = str(thread_id_obj) if isinstance(thread_id_obj, str) else "unknown"
            return f"thread.started thread_id={thread_id}"

        if event_type == "turn.started":
            return "turn.started"

        if event_type == "turn.completed":
            usage_obj = msg.get("usage")
            if isinstance(usage_obj, Mapping):
                input_tokens = _as_int_or_none(usage_obj.get("input_tokens"))
                cached_tokens = _as_int_or_none(usage_obj.get("cached_input_tokens"))
                output_tokens = _as_int_or_none(usage_obj.get("output_tokens"))
                if (
                    input_tokens is not None
                    and cached_tokens is not None
                    and output_tokens is not None
                ):
                    return (
                        "turn.completed "
                        f"usage in={input_tokens} cached={cached_tokens} out={output_tokens}"
                    )
            return "turn.completed"

        if event_type == "turn.failed":
            error_obj = msg.get("error")
            if isinstance(error_obj, Mapping):
                message_obj = error_obj.get("message")
                if isinstance(message_obj, str) and message_obj.strip():
                    return f"turn.failed message={self._clip(message_obj)}"
            return "turn.failed"

        if event_type == "error":
            message_obj = msg.get("message")
            if isinstance(message_obj, str) and message_obj.strip():
                return f"error message={self._clip(message_obj)}"
            return "error"

        if event_type in {"item.started", "item.updated", "item.completed"}:
            item_obj = msg.get("item")
            if not isinstance(item_obj, Mapping):
                return None
            item_summary = self._summarize_item_for_debug1(item_obj, event_type)
            if item_summary:
                return item_summary

        return None

    def _summarize_item_for_debug1(self, item: Mapping[str, object], event_type: str) -> str | None:
        item_type_obj = item.get("type")
        if not isinstance(item_type_obj, str) or not item_type_obj:
            return None

        item_id_obj = item.get("id")
        item_id = item_id_obj if isinstance(item_id_obj, str) and item_id_obj else "unknown"
        prefix = f"{event_type} {item_type_obj}#{item_id}"

        if item_type_obj == "agent_message":
            if event_type != "item.completed":
                return None
            text_obj = item.get("text")
            if isinstance(text_obj, str):
                return f"{prefix} chars={len(text_obj)}"
            return prefix

        if item_type_obj == "command_execution":
            status_obj = item.get("status")
            status = status_obj if isinstance(status_obj, str) else "unknown"
            command_obj = item.get("command")
            command = self._clip(command_obj) if isinstance(command_obj, str) else ""
            exit_code_obj = item.get("exit_code")
            exit_code = _as_int_or_none(exit_code_obj)
            summary = f"{prefix} status={status}"
            if command:
                summary += f" command={command}"
            if exit_code is not None:
                summary += f" exit_code={exit_code}"
            return summary

        if item_type_obj == "file_change":
            status_obj = item.get("status")
            status = status_obj if isinstance(status_obj, str) else "unknown"
            changes_obj = item.get("changes")
            count = len(changes_obj) if isinstance(changes_obj, list) else 0
            return f"{prefix} status={status} changes={count}"

        if item_type_obj == "mcp_tool_call":
            status_obj = item.get("status")
            status = status_obj if isinstance(status_obj, str) else "unknown"
            server_obj = item.get("server")
            tool_obj = item.get("tool")
            server = server_obj if isinstance(server_obj, str) else "?"
            tool = tool_obj if isinstance(tool_obj, str) else "?"
            return f"{prefix} status={status} server={server} tool={tool}"

        if item_type_obj == "web_search":
            query_obj = item.get("query")
            query = self._clip(query_obj) if isinstance(query_obj, str) else ""
            return f"{prefix} query={query}" if query else prefix

        if item_type_obj == "todo_list":
            items_obj = item.get("items")
            if isinstance(items_obj, list):
                total = len(items_obj)
                completed = sum(
                    1
                    for todo in items_obj
                    if isinstance(todo, Mapping) and todo.get("completed") is True
                )
                return f"{prefix} todos={completed}/{total}"
            return prefix

        if item_type_obj == "reasoning":
            text_obj = item.get("text")
            if isinstance(text_obj, str):
                return f"{prefix} chars={len(text_obj)}"
            return prefix

        if item_type_obj == "error":
            message_obj = item.get("message")
            if isinstance(message_obj, str):
                return f"{prefix} message={self._clip(message_obj)}"
            return prefix

        return prefix

    def _clip(self, text: str, max_chars: int = 120) -> str:
        stripped = " ".join(text.split())
        if len(stripped) <= max_chars:
            return stripped
        return stripped[: max_chars - 1] + "…"

    def _normalize_reasoning_effort(self, value: object, default: str) -> str:
        if not isinstance(value, str):
            return default
        raw = value.strip().lower()
        normalized = raw.replace("_", "")
        if normalized == "x-high":
            normalized = "xhigh"
        if normalized in _REASONING_EFFORT_VALUES:
            return normalized
        self._debug(1, f"Invalid reasoning effort '{value}', falling back to '{default}'")
        return default

    def _normalize_sandbox_mode(self, value: object, default: str) -> str:
        if not isinstance(value, str):
            return default
        normalized = value.strip().lower().replace("_", "-")
        if normalized in _SANDBOX_MODE_VALUES:
            return normalized
        self._debug(1, f"Invalid sandbox mode '{value}', falling back to '{default}'")
        return default

    def _resolve_api_key(self) -> str | None:
        # Prefer explicit CODEX_API_KEY, fallback to OPENAI_API_KEY.
        codex_api_key = os.environ.get("CODEX_API_KEY")
        if isinstance(codex_api_key, str):
            stripped = codex_api_key.strip()
            if stripped:
                return stripped

        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if isinstance(openai_api_key, str):
            stripped = openai_api_key.strip()
            if stripped:
                return stripped
        return None

    def _consume_turn(
        self,
        stream: Any,
        *,
        stream_enabled: bool,
        agent_message_state: dict[str, str],
    ) -> tuple[str | None, bool]:
        """Consume all events from a single turn.

        Returns (last_agent_message, parse_errors_seen).
        """
        last_agent_message: str | None = None
        buf_parts: list[str] = []
        printed_any = False
        parse_errors_seen = False

        try:
            for event in stream.events:
                result = self._handle_stream_event(
                    event=event,
                    stream_enabled=stream_enabled,
                    buf_parts=buf_parts,
                    agent_message_state=agent_message_state,
                )
                agent_msg = result.get("agent_message")
                if isinstance(agent_msg, str):
                    last_agent_message = agent_msg
                if result.get("printed"):
                    printed_any = True
                if result.get("task_complete"):
                    if stream_enabled and printed_any:
                        print("", file=sys.stdout, flush=True)
                    break
        except CodexParseError as parse_err:
            self._debug(1, f"[codex-event-parse-error] {parse_err}")
            parse_errors_seen = True

        if last_agent_message:
            return last_agent_message, parse_errors_seen

        combined = "".join(buf_parts).strip()
        if combined:
            return combined, parse_errors_seen

        return None, parse_errors_seen

    def execute(
        self,
        prompt: str,
        *,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        suppress_stream: bool = False,
        sandbox_mode: str = "read-only",
        output_schema: dict[str, object] | None = None,
        schema_prompt: str = "Produce the JSON output now.",
    ) -> str:
        """Execute Codex with the given prompt and return the response.

        model_name/reasoning_effort override the defaults for fast dedup passes.
        When suppress_stream is True, do not print streamed tokens to stdout.
        sandbox_mode: Codex sandbox policy (read-only, workspace-write, danger-full-access).
        output_schema: JSON Schema dict for structured output. When set, runs two
            turns: turn 1 executes the prompt (agentic tool use), turn 2 applies
            the schema to get guaranteed structured JSON.
        schema_prompt: Prompt for turn 2 when output_schema is set.
        """
        model = (model_name or self.config.model_name).strip()
        effort = self._normalize_reasoning_effort(
            reasoning_effort or self.config.reasoning_effort or "medium",
            "medium",
        )

        thread_options = ThreadOptions(
            model=model,
            sandbox_mode=cast(Any, self._normalize_sandbox_mode(sandbox_mode, "read-only")),
            model_reasoning_effort=cast(Any, effort),
        )

        stream_enabled = self._should_stream(suppress_stream)
        agent_message_state: dict[str, str] = {}

        try:
            client = Codex(
                options=CodexOptions(
                    config={"show_raw_agent_reasoning": False},
                    api_key=self._resolve_api_key(),
                )
            )
            thread = client.start_thread(thread_options)

            if output_schema:
                # Turn 1: agentic work (tool use, file reads, git diff)
                stream1 = thread.run_streamed(prompt)
                self._consume_turn(
                    stream1,
                    stream_enabled=stream_enabled,
                    agent_message_state=agent_message_state,
                )

                # Turn 2: structured output with schema enforcement
                turn_opts = TurnOptions(output_schema=output_schema)
                stream2 = thread.run_streamed(
                    schema_prompt,
                    turn_options=turn_opts,
                )
                result, _ = self._consume_turn(
                    stream2,
                    stream_enabled=False,
                    agent_message_state=agent_message_state,
                )
                if result:
                    return result
                raise CodexExecutionError("Codex did not return structured output on turn 2.")
            else:
                # Single-turn execution
                stream = thread.run_streamed(prompt)
                result, parse_errors_seen = self._consume_turn(
                    stream,
                    stream_enabled=stream_enabled,
                    agent_message_state=agent_message_state,
                )
                if result:
                    return result
                if parse_errors_seen:
                    self._debug(
                        1,
                        "[codex-event] no agent message; returning empty due to parse errors",
                    )
                    return ""
                raise CodexExecutionError("Codex did not return an agent message.")

        except ThreadRunError as run_err:
            raise CodexExecutionError(f"Codex execution failed: {run_err}") from run_err
        except CodexExecutionError:
            raise
        except Exception as e:
            raise CodexExecutionError(f"Codex execution failed: {e}") from e

    def _handle_stream_event(
        self,
        event: object,
        stream_enabled: bool,
        buf_parts: list[str],
        agent_message_state: dict[str, str],
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "agent_message": None,
            "printed": False,
            "task_complete": False,
        }
        if not isinstance(event, Mapping):
            return state

        event_type_obj = event.get("type")
        event_type = event_type_obj if isinstance(event_type_obj, str) else None
        self._emit_debug_event(event_type, event)

        if event_type == "error":
            message_obj = event.get("message")
            message = message_obj if isinstance(message_obj, str) else "Unknown error"
            raise CodexExecutionError(f"Codex error: {message}")

        if event_type == "turn.failed":
            error_obj = event.get("error")
            if isinstance(error_obj, dict):
                message_obj = error_obj.get("message")
                if isinstance(message_obj, str) and message_obj.strip():
                    raise CodexExecutionError(f"Codex error: {message_obj}")
            raise CodexExecutionError("Codex error: turn failed")

        if event_type == "turn.completed":
            state["task_complete"] = True
            return state

        if event_type not in {"item.updated", "item.completed"}:
            return state

        item_obj = event.get("item")
        if not isinstance(item_obj, Mapping):
            return state

        if item_obj.get("type") != "agent_message":
            return state

        text_obj = item_obj.get("text")
        if not isinstance(text_obj, str):
            return state
        item_id_obj = item_obj.get("id")
        item_id = item_id_obj if isinstance(item_id_obj, str) and item_id_obj else "agent_message"
        previous_text = agent_message_state.get(item_id, "")
        delta = text_obj
        if previous_text:
            if text_obj.startswith(previous_text):
                delta = text_obj[len(previous_text) :]
            elif text_obj == previous_text:
                delta = ""
        agent_message_state[item_id] = text_obj

        state["agent_message"] = text_obj
        if delta:
            buf_parts.append(delta)
            if stream_enabled:
                print(delta, end="", flush=True)
                state["printed"] = True
        return state
