from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from codex import Codex, CodexOptions, ThreadResumeOptions, ThreadStartOptions, TurnOptions
from codex.errors import CodexParseError, ThreadRunError
from codex.protocol import types as protocol
from codex.thread import CodexTurnStream, Thread
from pydantic import BaseModel

from ..core.config import ReviewConfig, make_debug
from ..core.exceptions import CodexExecutionError
from .codex_event_debugger import CodexEventDebugger

_REASONING_EFFORT_VALUES = {"minimal", "low", "medium", "high", "xhigh"}
_SANDBOX_MODE_VALUES = {"read-only", "workspace-write", "danger-full-access"}
_WEB_SEARCH_MODE_VALUES = ("disabled", "cached", "live")


@dataclass
class _StreamingAgentMessageState:
    last_text_by_item_id: dict[str, str] = field(default_factory=dict)
    last_agent_message: str | None = None
    buffered_text_parts: list[str] = field(default_factory=list)
    printed_output: bool = False

    def append_agent_message_delta(
        self,
        *,
        item_id: str,
        text: str,
        stream_enabled: bool,
    ) -> bool:
        previous_text = self.last_text_by_item_id.get(item_id, "")
        delta = self._message_delta(previous_text=previous_text, text=text)
        self.last_text_by_item_id[item_id] = text
        self.last_agent_message = text

        if not delta:
            return False

        self.buffered_text_parts.append(delta)
        self.printed_output = self.printed_output or stream_enabled
        return True

    def append_agent_message_chunk(
        self,
        *,
        item_id: str,
        chunk: str,
        stream_enabled: bool,
    ) -> bool:
        if not chunk:
            return False
        previous_text = self.last_text_by_item_id.get(item_id, "")
        current_text = previous_text + chunk
        self.last_text_by_item_id[item_id] = current_text
        self.last_agent_message = current_text
        self.buffered_text_parts.append(chunk)
        self.printed_output = self.printed_output or stream_enabled
        return True

    def _message_delta(self, *, previous_text: str, text: str) -> str:
        if not previous_text:
            return text
        if text == previous_text:
            return ""
        if text.startswith(previous_text):
            return text[len(previous_text) :]
        return text


class CodexClient:
    """Client for executing Codex with typed streaming and response parsing."""

    def __init__(self, config: ReviewConfig) -> None:
        self.config = config
        self._debug = make_debug(config)
        self._event_debugger = CodexEventDebugger(
            debug_level=config.debug_level,
            debug_fn=self._debug,
        )

    def execute_text(
        self,
        prompt: str,
        *,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        suppress_stream: bool = False,
        sandbox_mode: str = "read-only",
        resume_thread_id: str | None = None,
    ) -> str:
        """Run a single text turn and return the final agent text."""
        return self._run_session(
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            suppress_stream=suppress_stream,
            sandbox_mode=sandbox_mode,
            resume_thread_id=resume_thread_id,
            session_runner=lambda thread, effort, stream_enabled: self._run_text_session(
                thread=thread,
                prompt=prompt,
                effort=effort,
                stream_enabled=stream_enabled,
            ),
        )

    def execute_structured(
        self,
        prompt: str,
        *,
        output_schema: dict[str, object],
        schema_prompt: str = "Produce the JSON output now.",
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        suppress_stream: bool = False,
        sandbox_mode: str = "read-only",
        resume_thread_id: str | None = None,
    ) -> str:
        """Run an agentic turn followed by a schema-enforced output turn."""
        return self._run_session(
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            suppress_stream=suppress_stream,
            sandbox_mode=sandbox_mode,
            resume_thread_id=resume_thread_id,
            session_runner=lambda thread, effort, stream_enabled: self._run_structured_session(
                thread=thread,
                prompt=prompt,
                output_schema=output_schema,
                schema_prompt=schema_prompt,
                effort=effort,
                stream_enabled=stream_enabled,
            ),
        )

    def _run_session(
        self,
        *,
        model_name: str | None,
        reasoning_effort: str | None,
        suppress_stream: bool,
        sandbox_mode: str,
        resume_thread_id: str | None,
        session_runner: Callable[[Thread, str, bool], str],
    ) -> str:
        effort = self._resolve_effort(reasoning_effort)
        stream_enabled = self._should_stream(suppress_stream)

        try:
            thread = self._start_or_resume_thread(
                model_name=model_name,
                sandbox_mode=sandbox_mode,
                resume_thread_id=resume_thread_id,
            )
            return session_runner(thread, effort, stream_enabled)
        except ThreadRunError as run_err:
            raise CodexExecutionError(f"Codex execution failed: {run_err}") from run_err
        except CodexExecutionError:
            raise
        except Exception as exc:
            raise CodexExecutionError(f"Codex execution failed: {exc}") from exc

    def _run_text_session(
        self,
        *,
        thread: Thread,
        prompt: str,
        effort: str,
        stream_enabled: bool,
    ) -> str:
        streaming_state = _StreamingAgentMessageState()
        result, parse_errors_seen = self._run_turn(
            thread=thread,
            prompt=prompt,
            effort=effort,
            stream_enabled=stream_enabled,
            streaming_state=streaming_state,
        )
        return self._require_turn_result(
            result=result,
            parse_errors_seen=parse_errors_seen,
            missing_output_message="Codex did not return an agent message.",
        )

    def _run_structured_session(
        self,
        *,
        thread: Thread,
        prompt: str,
        output_schema: dict[str, object],
        schema_prompt: str,
        effort: str,
        stream_enabled: bool,
    ) -> str:
        interactive_turn_state = _StreamingAgentMessageState()
        self._run_turn(
            thread=thread,
            prompt=prompt,
            effort=effort,
            stream_enabled=stream_enabled,
            streaming_state=interactive_turn_state,
        )
        schema_turn_state = _StreamingAgentMessageState()
        result, parse_errors_seen = self._run_turn(
            thread=thread,
            prompt=schema_prompt,
            effort=effort,
            stream_enabled=False,
            streaming_state=schema_turn_state,
            output_schema=output_schema,
        )
        return self._require_turn_result(
            result=result,
            parse_errors_seen=parse_errors_seen,
            missing_output_message="Codex did not return structured output on turn 2.",
        )

    def _run_turn(
        self,
        *,
        thread: Thread,
        prompt: str,
        effort: str,
        stream_enabled: bool,
        streaming_state: _StreamingAgentMessageState,
        output_schema: dict[str, object] | None = None,
    ) -> tuple[str | None, bool]:
        stream = thread.run(
            prompt,
            turn_options=self._turn_options(
                effort=effort,
                output_schema=output_schema,
            ),
        )
        return self._consume_turn(
            stream,
            stream_enabled=stream_enabled,
            streaming_state=streaming_state,
        )

    def _consume_turn(
        self,
        stream: CodexTurnStream,
        *,
        stream_enabled: bool,
        streaming_state: _StreamingAgentMessageState,
    ) -> tuple[str | None, bool]:
        parse_errors_seen = False

        try:
            for event in stream:
                result = self._handle_stream_event(
                    event=event,
                    stream_enabled=stream_enabled,
                    streaming_state=streaming_state,
                )
                if result.get("task_complete"):
                    if stream_enabled and streaming_state.printed_output:
                        print("", file=sys.stdout, flush=True)
                    break
            stream.wait()
        except CodexParseError as parse_err:
            self._debug(1, f"[codex-event-parse-error] {parse_err}")
            parse_errors_seen = True

        if not parse_errors_seen:
            final_text = stream.final_text.strip()
            if final_text:
                return final_text, False

        if streaming_state.last_agent_message:
            return streaming_state.last_agent_message, parse_errors_seen

        combined = "".join(streaming_state.buffered_text_parts).strip()
        if combined:
            return combined, parse_errors_seen

        return None, parse_errors_seen

    def _handle_stream_event(
        self,
        *,
        event: BaseModel,
        stream_enabled: bool,
        streaming_state: _StreamingAgentMessageState,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "task_complete": False,
        }
        self._emit_debug_event(event)

        if isinstance(event, protocol.ItemAgentMessageDeltaNotification):
            self._handle_agent_message_delta_event(
                event=event,
                stream_enabled=stream_enabled,
                streaming_state=streaming_state,
            )
            return state

        if isinstance(event, protocol.ErrorNotificationModel):
            raise CodexExecutionError(f"Codex error: {event.params.error.message}")

        if isinstance(event, protocol.TurnCompletedNotificationModel):
            state["task_complete"] = True
            self._handle_turn_completion_event(event)
            return state

        if isinstance(event, protocol.ItemCompletedNotificationModel):
            self._handle_item_completed_event(
                event=event,
                stream_enabled=stream_enabled,
                streaming_state=streaming_state,
            )
            return state

        return state

    def _handle_agent_message_delta_event(
        self,
        *,
        event: protocol.ItemAgentMessageDeltaNotification,
        stream_enabled: bool,
        streaming_state: _StreamingAgentMessageState,
    ) -> None:
        chunk = event.params.delta
        if not streaming_state.append_agent_message_chunk(
            item_id=event.params.itemId,
            chunk=chunk,
            stream_enabled=stream_enabled,
        ):
            return
        if stream_enabled:
            print(chunk, end="", flush=True)
            streaming_state.printed_output = True

    def _handle_turn_completion_event(self, event: protocol.TurnCompletedNotificationModel) -> None:
        status = event.params.turn.status.root
        if status == "failed":
            error = event.params.turn.error
            if error is not None and error.message.strip():
                raise CodexExecutionError(f"Codex error: {error.message}")
            raise CodexExecutionError("Codex error: turn failed")
        if status == "interrupted":
            raise CodexExecutionError("Codex error: turn interrupted")

    def _handle_item_completed_event(
        self,
        *,
        event: protocol.ItemCompletedNotificationModel,
        stream_enabled: bool,
        streaming_state: _StreamingAgentMessageState,
    ) -> None:
        item = event.params.item.root
        if not isinstance(item, protocol.AgentMessageThreadItem):
            return
        self._append_agent_message_delta(
            item_id=item.id,
            text=item.text,
            stream_enabled=stream_enabled,
            streaming_state=streaming_state,
        )

    def _append_agent_message_delta(
        self,
        *,
        item_id: str,
        text: str,
        stream_enabled: bool,
        streaming_state: _StreamingAgentMessageState,
    ) -> None:
        delta_was_appended = streaming_state.append_agent_message_delta(
            item_id=item_id,
            text=text,
            stream_enabled=stream_enabled,
        )
        if delta_was_appended and stream_enabled:
            delta = streaming_state.buffered_text_parts[-1]
            print(delta, end="", flush=True)

    def _emit_debug_event(self, event: BaseModel) -> None:
        self._event_debugger.emit(event)

    def _should_stream(self, suppress_stream: bool) -> bool:
        return bool(self.config.stream_output and not suppress_stream)

    def _resolve_effort(self, reasoning_effort: str | None) -> str:
        return self._normalize_reasoning_effort(
            reasoning_effort or self.config.reasoning_effort or "medium",
            "medium",
        )

    def _normalize_reasoning_effort(self, value: object, default: str) -> str:
        if not isinstance(value, str):
            return default
        normalized = value.strip().lower().replace("_", "")
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
        resolved_api_key = self.config.openai_api_key.strip()
        return resolved_api_key or None

    def _codex_web_search_mode(self) -> Literal["disabled", "cached", "live"]:
        mode = self.config.web_search_mode or "live"
        if mode in _WEB_SEARCH_MODE_VALUES:
            return cast(Literal["disabled", "cached", "live"], mode)
        self._debug(1, f"Invalid web search mode '{mode}', falling back to 'live'")
        return "live"

    def _codex_process_env(self) -> dict[str, str] | None:
        codex_home = os.environ.get("CODEX_HOME")
        if not isinstance(codex_home, str):
            return None
        normalized = codex_home.strip()
        if not normalized:
            return None
        return {"CODEX_HOME": normalized}

    def _resolved_model_name(self, model_name: str | None) -> str:
        resolved_model_name = model_name if model_name is not None else self.config.model_name
        return resolved_model_name.strip()

    def _make_codex_client(self) -> Codex:
        return Codex(
            options=CodexOptions(
                config=cast(Any, {"show_raw_agent_reasoning": self.config.debug_level >= 2}),
                api_key=self._resolve_api_key(),
                env=self._codex_process_env(),
            )
        )

    def _thread_config(self) -> dict[str, Literal["disabled", "cached", "live"]]:
        return {"web_search": self._codex_web_search_mode()}

    def _start_or_resume_thread(
        self,
        *,
        model_name: str | None,
        sandbox_mode: str,
        resume_thread_id: str | None,
    ) -> Thread:
        resolved_sandbox_mode = self._normalize_sandbox_mode(sandbox_mode, "read-only")
        resolved_model_name = self._resolved_model_name(model_name)
        codex_client = self._make_codex_client()
        if resume_thread_id:
            try:
                self._debug(1, f"Attempting to resume Codex thread {resume_thread_id}")
                return codex_client.resume_thread(
                    resume_thread_id,
                    ThreadResumeOptions(
                        model=resolved_model_name,
                        sandbox=cast(Any, resolved_sandbox_mode),
                        config=cast(Any, self._thread_config()),
                    ),
                )
            except Exception as exc:
                self._debug(
                    1,
                    f"Failed to resume Codex thread {resume_thread_id}: {exc}; starting fresh",
                )
        return codex_client.start_thread(
            ThreadStartOptions(
                model=resolved_model_name,
                sandbox=cast(Any, resolved_sandbox_mode),
                config=cast(Any, self._thread_config()),
            )
        )

    def _turn_options(
        self,
        *,
        effort: str,
        output_schema: dict[str, object] | None = None,
    ) -> TurnOptions:
        return TurnOptions(
            effort=cast(Any, effort),
            output_schema=output_schema,
        )

    def _require_turn_result(
        self,
        *,
        result: str | None,
        parse_errors_seen: bool,
        missing_output_message: str,
    ) -> str:
        if result:
            return result
        if parse_errors_seen:
            raise CodexExecutionError("Codex stream parsing failed before producing output.")
        raise CodexExecutionError(missing_output_message)
