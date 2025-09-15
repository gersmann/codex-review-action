from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from typing import Any, cast

from codex import CodexClient as CoreCodexClient
from codex import CodexConfig
from codex.config import ApprovalPolicy, ReasoningEffort, SandboxMode
from codex.event import Event
from codex.protocol.types import (
    EventMsgAgentMessage,
    EventMsgAgentMessageDelta,
    EventMsgAgentReasoningDelta,
    EventMsgError,
    EventMsgTaskComplete,
)

from .config import ReviewConfig
from .exceptions import CodexExecutionError


class CodexClient:
    """Client for executing Codex with streaming and response parsing."""

    def __init__(self, config: ReviewConfig) -> None:
        self.config = config

    def _debug(self, level: int, message: str) -> None:
        if self.config.debug_level >= level:
            print(f"[debug{level}] {message}", file=sys.stderr)

    # -------- Internal helpers (control flow + I/O) --------
    def _should_stream(self, suppress_stream: bool) -> bool:
        """Return True if stdout streaming is enabled for this call."""
        return bool(self.config.stream_output and not suppress_stream)

    def _emit_debug_delta(self, msg: dict[str, Any]) -> None:
        """Emit condensed agent deltas to stderr when debug is on."""
        if self.config.debug_level < 1:
            return
        d = msg.get("delta")
        if isinstance(d, str):
            d_one_line = d.replace("\n", "").replace("\r", "")
            print(d_one_line, end="", file=sys.stderr)

    def _emit_debug_event(self, msg_type: str | None, msg: dict[str, Any] | None) -> None:
        if self.config.debug_level < 1:
            return
        if not isinstance(msg, dict):
            self._debug(1, f"[codex-event] {msg_type}: {msg}")
            return
        if msg_type in ("error", "stream_error", "background_event"):
            detail = msg.get("message")
            if detail:
                self._debug(1, f"[codex-event] {msg_type}: {detail}")
                return
        self._debug(1, f"[codex-event] {msg_type}: {msg}")

    def execute(
        self,
        prompt: str,
        *,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        suppress_stream: bool = False,
        config_overrides: dict[str, Any] | None = None,
    ) -> str:
        """Execute Codex with the given prompt and return the response.

        model_name/reasoning_effort override the defaults for fast dedup passes.
        When suppress_stream is True, do not print streamed tokens to stdout.
        config_overrides: Additional config overrides to merge with defaults.
        """
        # Resolve model/effort once, up front.
        model = model_name or self.config.model_name
        effort_str = (reasoning_effort or self.config.reasoning_effort or "").lower() or "medium"
        try:
            effort_enum: ReasoningEffort | None = ReasoningEffort(effort_str)
        except ValueError:
            effort_enum = None

        # Build base config
        base_config: dict[str, Any] = {
            "approval_policy": ApprovalPolicy.NEVER,
            "sandbox_mode": SandboxMode.READ_ONLY,
            "include_plan_tool": True,
            "include_apply_patch_tool": False,
            "include_view_image_tool": False,
            "show_raw_agent_reasoning": False,
            "model": model,
            "model_reasoning_effort": effort_enum,
            "model_provider": self.config.model_provider,
            "mcp_servers": {},
            "tools": {"web_search": False, "view_image": False},
        }

        # Merge in any provided overrides
        if config_overrides:
            base_config.update(config_overrides)

        # Use pydantic validation with a Mapping to avoid overly strict mypy kwarg checks
        overrides = CodexConfig.model_validate(cast(Mapping[str, Any], base_config))

        last_msg: str | None = None
        buf_parts: list[str] = []
        stream_enabled = self._should_stream(suppress_stream)
        # Track whether we've printed any content to decide on newlines cleanly.
        printed_any = False
        parse_errors_seen = False

        try:
            client = CoreCodexClient(load_default_config=False, config=overrides)
            conversation = client.start_conversation(
                prompt,
                load_default_config=False,
            )

            # Iterate defensively: tolerate parser/validation errors from SDK.
            it = iter(conversation)
            while True:
                try:
                    event = next(it)
                except StopIteration:
                    break
                except Exception as parse_err:
                    self._debug(1, f"[codex-event-parse-error] {parse_err}")
                    parse_errors_seen = True
                    continue

                result = self._handle_conversation_event(event, stream_enabled, buf_parts)
                val = result.get("last_msg")
                if isinstance(val, str) or val is None:
                    last_msg = val
                if result.get("printed"):
                    printed_any = True
                if result.get("task_complete") and stream_enabled and printed_any:
                    print("", file=sys.stdout, flush=True)

        except Exception as e:
            raise CodexExecutionError(f"Codex execution failed: {e}") from e

        if last_msg:
            return last_msg

        combined = "".join(buf_parts).strip()
        if combined:
            return combined
        if parse_errors_seen:
            # Be permissive: return empty output so callers can still proceed
            # (e.g., check git changes) rather than failing the whole run.
            self._debug(1, "[codex-event] no agent message; returning empty due to parse errors")
            return ""
        raise CodexExecutionError("Codex did not return an agent message.")

    def parse_json_response(self, text: str) -> dict[str, Any]:
        """Parse a JSON object from model output that may include code fences or extra text.

        Strategy:
        1) If fenced with ``` or ```json, strip the fences and parse.
        2) Otherwise, find the first '{' and the last '}' and attempt to parse that slice.
        3) Raise JSONDecodeError if still invalid.
        """
        s = text.strip()
        fence_match = re.match(r"^```(?:json)?\n([\s\S]*?)\n```\s*$", s)
        if fence_match:
            inner = fence_match.group(1)
            obj = json.loads(inner)
            if not isinstance(obj, dict):
                raise json.JSONDecodeError("Top-level JSON is not an object", inner, 0)
            return cast(dict[str, Any], obj)

        # Fallback: extract the outermost JSON object by slicing
        first = s.find("{")
        last = s.rfind("}")
        if first != -1 and last != -1 and last > first:
            candidate = s[first : last + 1]
            obj = json.loads(candidate)
            if not isinstance(obj, dict):
                raise json.JSONDecodeError("Top-level JSON is not an object", candidate, 0)
            return cast(dict[str, Any], obj)

        # Final attempt: remove any lone fences that didn't match above
        s2 = re.sub(r"^```.*?$|```$", "", s, flags=re.MULTILINE).strip()
        obj = json.loads(s2)
        if not isinstance(obj, dict):
            raise json.JSONDecodeError("Top-level JSON is not an object", s2, 0)
        return cast(dict[str, Any], obj)

    # -------- Extracted loop body helper --------
    def _handle_conversation_event(
        self, event: Any, stream_enabled: bool, buf_parts: list[str]
    ) -> dict[str, Any]:
        """Handle a single conversation event and return state deltas.

        Returns a dict with optional keys:
        - "last_msg": str | None — last full agent message seen
        - "printed": bool — whether anything was printed to stdout
        - "task_complete": bool — whether a task-complete event occurred
        May raise CodexExecutionError on error events.
        """
        state: dict[str, Any] = {"last_msg": None, "printed": False, "task_complete": False}

        if not isinstance(event, Event):
            return state

        # event.msg can be a union of EventMsg and AnyEventMsg; treat dynamically
        msg: Any = event.msg
        inner = getattr(msg, "root", msg)

        if isinstance(inner, (EventMsgAgentMessageDelta, EventMsgAgentReasoningDelta)):
            self._emit_debug_delta({"delta": getattr(inner, "delta", "")})
        else:
            # Use model_dump() to avoid pydantic repr noise
            try:
                payload = inner.model_dump()
            except Exception:
                payload = None
            self._emit_debug_event(getattr(inner, "type", None), payload)

        if isinstance(inner, EventMsgAgentMessage):
            state["last_msg"] = inner.message
            buf_parts.append(inner.message)
            if stream_enabled:
                print(inner.message, end="", flush=True)
                state["printed"] = True
            return state

        if isinstance(inner, EventMsgAgentMessageDelta):
            buf_parts.append(inner.delta)
            if stream_enabled:
                print(inner.delta, end="", flush=True)
                state["printed"] = True
            return state

        if isinstance(inner, EventMsgTaskComplete):
            if inner.last_agent_message:
                state["last_msg"] = inner.last_agent_message
            state["task_complete"] = True
            return state

        if isinstance(inner, EventMsgError):
            raise CodexExecutionError(f"Codex error: {inner.message}")

        return state
