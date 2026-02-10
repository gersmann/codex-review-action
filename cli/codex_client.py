from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, cast

from codex import Codex, CodexOptions, ThreadOptions
from codex.errors import CodexParseError, ThreadRunError

from .config import ReviewConfig
from .exceptions import CodexExecutionError

_REASONING_EFFORT_VALUES = {"minimal", "low", "medium", "high", "xhigh"}
_SANDBOX_MODE_VALUES = {"read-only", "workspace-write", "danger-full-access"}
_WEB_SEARCH_MODE_VALUES = {"disabled", "cached", "live"}
_APPROVAL_POLICY_VALUES = {"never", "on-request", "on-failure", "untrusted"}


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

    def _emit_debug_event(self, msg_type: str | None, msg: object) -> None:
        if self.config.debug_level < 1:
            return
        if isinstance(msg, dict):
            event_type_obj = msg.get("type")
            event_type = (
                str(event_type_obj) if isinstance(event_type_obj, str) else (msg_type or "")
            )
            if event_type == "item.updated":
                item_obj = msg.get("item")
                if isinstance(item_obj, dict) and item_obj.get("type") == "agent_message":
                    return
                self._debug(2, f"[codex-event] {event_type}: {msg}")
                return

            if event_type in {"error", "turn.failed"}:
                self._debug(1, f"[codex-event] {event_type}: {msg}")
                return
            self._debug(2, f"[codex-event] {event_type}: {msg}")
            return

        self._debug(1, f"[codex-event] {msg_type}: {msg}")

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

    def _normalize_web_search_mode(self, value: object, default: str) -> str:
        if not isinstance(value, str):
            return default
        normalized = value.strip().lower().replace("_", "-")
        if normalized in _WEB_SEARCH_MODE_VALUES:
            return normalized
        self._debug(1, f"Invalid web search mode '{value}', falling back to '{default}'")
        return default

    def _normalize_approval_policy(self, value: object, default: str) -> str:
        if not isinstance(value, str):
            return default
        normalized = value.strip().lower().replace("_", "-")
        if normalized in _APPROVAL_POLICY_VALUES:
            return normalized
        self._debug(1, f"Invalid approval policy '{value}', falling back to '{default}'")
        return default

    def _coerce_optional_bool(self, value: object) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return None

    def _coerce_optional_str(self, value: object) -> str | None:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        return None

    def _coerce_optional_str_list(self, value: object) -> list[str] | None:
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else None
        if not isinstance(value, Sequence):
            return None
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    out.append(stripped)
        return out or None

    def _drop_nones(self, value: object) -> object:
        if isinstance(value, dict):
            out: dict[str, object] = {}
            for k, v in value.items():
                if v is None:
                    continue
                cleaned = self._drop_nones(v)
                if cleaned is not None:
                    out[k] = cleaned
            return out
        if isinstance(value, list):
            out_list: list[object] = []
            for item in value:
                if item is None:
                    continue
                cleaned = self._drop_nones(item)
                if cleaned is not None:
                    out_list.append(cleaned)
            return out_list
        return value

    def _build_thread_options(
        self,
        *,
        model: str,
        reasoning_effort: str,
        overrides: dict[str, Any],
    ) -> tuple[ThreadOptions, dict[str, Any]]:
        effective = dict(overrides)
        model_override = self._coerce_optional_str(effective.pop("model", None))
        resolved_model = model_override or model

        sandbox_mode = self._normalize_sandbox_mode(
            effective.pop("sandbox_mode", "read-only"), "read-only"
        )
        resolved_reasoning_effort = self._normalize_reasoning_effort(
            effective.pop("model_reasoning_effort", reasoning_effort),
            reasoning_effort,
        )
        approval_policy = self._normalize_approval_policy(
            effective.pop("approval_policy", "never"), "never"
        )
        web_search_mode = self._normalize_web_search_mode(
            effective.pop("web_search_mode", "live"), "live"
        )
        web_search_enabled = self._coerce_optional_bool(effective.pop("web_search_enabled", None))
        network_access_enabled = self._coerce_optional_bool(
            effective.pop("network_access_enabled", None)
        )
        skip_git_repo_check = self._coerce_optional_bool(effective.pop("skip_git_repo_check", None))
        working_directory = self._coerce_optional_str(effective.pop("working_directory", None))
        additional_directories = self._coerce_optional_str_list(
            effective.pop("additional_directories", None)
        )

        thread_options = ThreadOptions(
            model=resolved_model,
            sandbox_mode=cast(Any, sandbox_mode),
            working_directory=working_directory,
            skip_git_repo_check=bool(skip_git_repo_check)
            if skip_git_repo_check is not None
            else False,
            model_reasoning_effort=cast(Any, resolved_reasoning_effort),
            network_access_enabled=network_access_enabled,
            web_search_mode=cast(Any, web_search_mode),
            web_search_enabled=web_search_enabled,
            approval_policy=cast(Any, approval_policy),
            additional_directories=additional_directories,
        )
        cleaned_overrides = self._drop_nones(effective)
        if not isinstance(cleaned_overrides, dict):
            cleaned_overrides = {}
        return thread_options, cast(dict[str, Any], cleaned_overrides)

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
        model = (model_name or self.config.model_name).strip()
        effort = self._normalize_reasoning_effort(
            reasoning_effort or self.config.reasoning_effort or "medium",
            "medium",
        )

        base_overrides: dict[str, Any] = {
            "include_plan_tool": True,
            "include_view_image_tool": False,
            "show_raw_agent_reasoning": False,
        }
        if config_overrides:
            base_overrides.update(config_overrides)

        thread_options, sdk_overrides = self._build_thread_options(
            model=model,
            reasoning_effort=effort,
            overrides=base_overrides,
        )

        last_msg: str | None = None
        last_agent_message: str | None = None
        buf_parts: list[str] = []
        stream_enabled = self._should_stream(suppress_stream)
        printed_any = False
        parse_errors_seen = False
        agent_message_state: dict[str, str] = {}
        try:
            client = Codex(
                options=CodexOptions(
                    config=cast(Any, sdk_overrides),
                    api_key=self._resolve_api_key(),
                )
            )
            thread = client.start_thread(thread_options)
            stream = thread.run_streamed(prompt)
            for event in stream.events:
                result = self._handle_stream_event(
                    event=event,
                    stream_enabled=stream_enabled,
                    buf_parts=buf_parts,
                    agent_message_state=agent_message_state,
                )
                val = result.get("last_msg")
                if isinstance(val, str) or val is None:
                    last_msg = val
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
        except ThreadRunError as run_err:
            raise CodexExecutionError(f"Codex execution failed: {run_err}") from run_err
        except CodexExecutionError:
            raise
        except Exception as e:
            raise CodexExecutionError(f"Codex execution failed: {e}") from e

        if last_agent_message:
            return last_agent_message

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

    def _handle_stream_event(
        self,
        event: object,
        stream_enabled: bool,
        buf_parts: list[str],
        agent_message_state: dict[str, str],
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "last_msg": None,
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

        state["last_msg"] = text_obj
        state["agent_message"] = text_obj
        if delta:
            buf_parts.append(delta)
            if stream_enabled:
                print(delta, end="", flush=True)
                state["printed"] = True
        return state
