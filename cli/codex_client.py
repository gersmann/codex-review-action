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
        event_type = msg_type or ""
        if isinstance(msg, dict):
            event_type = str(msg.get("type") or event_type)
        event_type_lower = event_type.lower()
        if "exec_command" in event_type_lower and "delta" in event_type_lower:
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
            "tools": {"web_search": True, "view_image": False},
        }

        # Merge in any provided overrides
        if config_overrides:
            base_config.update(config_overrides)

        # Use pydantic validation with a Mapping to avoid overly strict mypy kwarg checks
        overrides = CodexConfig.model_validate(cast(Mapping[str, Any], base_config))

        last_msg: str | None = None
        last_agent_message: str | None = None
        last_task_message: str | None = None
        buf_parts: list[str] = []
        stream_enabled = self._should_stream(suppress_stream)
        # Track whether we've printed any content to decide on newlines cleanly.
        printed_any = False
        parse_errors_seen = False

        conversation = None
        try:
            client = CoreCodexClient(load_default_config=False, config=overrides)
            conversation = client.start_conversation(load_default_config=False)
            conversation.submit_user_turn(prompt)

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
                agent_msg = result.get("agent_message")
                if isinstance(agent_msg, str):
                    last_agent_message = agent_msg
                task_msg = result.get("task_message")
                if isinstance(task_msg, str):
                    last_task_message = task_msg
                if result.get("printed"):
                    printed_any = True
                if result.get("task_complete"):
                    if stream_enabled and printed_any:
                        print("", file=sys.stdout, flush=True)
                    break

        except Exception as e:
            raise CodexExecutionError(f"Codex execution failed: {e}") from e
        finally:
            try:
                if conversation is not None:
                    conversation.shutdown()
            except Exception:
                pass

        if last_agent_message:
            return last_agent_message

        if last_task_message:
            return last_task_message

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
        """Parse a JSON object from model output that may include fences or extra text.

        Strategy, in order:
        1) If fenced with ``` or ```json, strip the fences and parse.
        2) Scan for the first balanced top-level JSON object (brace counting) and parse it.
        3) As a final fallback, attempt a broad slice from the first '{' to the last '}'.
        """
        s = text.strip()
        fence_match = re.match(r"^```(?:json)?\n([\s\S]*?)\n```\s*$", s)
        if fence_match:
            inner = fence_match.group(1)
            obj = json.loads(inner)
            if not isinstance(obj, dict):
                raise json.JSONDecodeError("Top-level JSON is not an object", inner, 0)
            return cast(dict[str, Any], obj)

        # Robust extractor: find first balanced top-level JSON object
        start = s.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(s)):
                ch = s[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = s[start : i + 1]
                        try:
                            obj = json.loads(candidate)
                            if isinstance(obj, dict):
                                return cast(dict[str, Any], obj)
                        except json.JSONDecodeError:
                            pass
                        break

        # Fallback: extract the outermost JSON slice (may succeed on simple cases)
        first = s.find("{")
        last = s.rfind("}")
        if first != -1 and last != -1 and last > first:
            candidate2 = s[first : last + 1]
            try:
                obj2 = json.loads(candidate2)
            except json.JSONDecodeError:
                obj2 = None
            if isinstance(obj2, dict):
                return cast(dict[str, Any], obj2)

        # Attempt to decode the first JSON object even when extra data follows (e.g., duplicate payloads)
        decoder = json.JSONDecoder()
        stripped = s.lstrip()
        try:
            obj3, _ = decoder.raw_decode(stripped)
            if isinstance(obj3, dict):
                return cast(dict[str, Any], obj3)
        except json.JSONDecodeError:
            brace_idx = stripped.find("{")
            if brace_idx > 0:
                try:
                    obj4, _ = decoder.raw_decode(stripped[brace_idx:])
                    if isinstance(obj4, dict):
                        return cast(dict[str, Any], obj4)
                except json.JSONDecodeError:
                    pass

        # Final attempt: strip dangling fences and parse whole string
        s2 = re.sub(r"^```.*?$|```$", "", s, flags=re.MULTILINE).strip()
        obj3 = json.loads(s2)
        if not isinstance(obj3, dict):
            raise json.JSONDecodeError("Top-level JSON is not an object", s2, 0)
        return cast(dict[str, Any], obj3)

    # -------- Extracted loop body helper --------
    def _handle_conversation_event(
        self, event: Any, stream_enabled: bool, buf_parts: list[str]
    ) -> dict[str, Any]:
        """Handle a single conversation event and return state deltas.

        Returns a dict with optional keys:
        - "last_msg": str | None — last agent- or task-complete message seen
        - "agent_message": str | None — last EventMsgAgentMessage payload
        - "task_message": str | None — task-complete last_agent_message payload
        - "printed": bool — whether anything was printed to stdout
        - "task_complete": bool — whether a task-complete event occurred
        May raise CodexExecutionError on error events.
        """
        state: dict[str, Any] = {
            "last_msg": None,
            "agent_message": None,
            "task_message": None,
            "printed": False,
            "task_complete": False,
        }

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
            state["agent_message"] = inner.message
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
                state["task_message"] = inner.last_agent_message
            state["task_complete"] = True
            return state

        if isinstance(inner, EventMsgError):
            raise CodexExecutionError(f"Codex error: {inner.message}")

        return state
