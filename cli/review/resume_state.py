from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SUMMARY_METADATA_RE = re.compile(r"<!--\s*codex-review-meta\s+({.*?})\s*-->")
SESSION_INDEX_PATH = "session_index.jsonl"
ROLLOUTS_SUBDIR = "sessions"
REVIEW_RESUME_CACHE_VERSION = "v1"
MAX_INLINE_INCREMENTAL_DIFF_LINES = 500


def render_review_summary_metadata(reviewed_head_sha: str) -> str:
    payload = json.dumps(
        {"reviewed_head_sha": reviewed_head_sha},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"<!-- codex-review-meta {payload} -->"


def parse_reviewed_head_sha(summary_body: str) -> str | None:
    match = SUMMARY_METADATA_RE.search(summary_body)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    reviewed_head_sha = payload.get("reviewed_head_sha")
    if not isinstance(reviewed_head_sha, str):
        return None
    normalized = reviewed_head_sha.strip()
    return normalized or None


def compute_review_cache_key(
    repository: str,
    pr_number: int,
    model_name: str,
    reviewed_head_sha: str,
) -> str:
    sanitized_repository = _sanitize_cache_component(repository)
    sanitized_model_name = _sanitize_cache_component(model_name)
    sanitized_sha = _sanitize_cache_component(reviewed_head_sha)
    return (
        f"codex-review-{REVIEW_RESUME_CACHE_VERSION}-"
        f"{sanitized_repository}-pr-{pr_number}-{sanitized_model_name}-{sanitized_sha}"
    )


def extract_current_head_sha(event: Mapping[str, Any]) -> str:
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, Mapping):
        return ""
    head = pull_request.get("head")
    if not isinstance(head, Mapping):
        return ""
    current_head_sha = head.get("sha")
    if not isinstance(current_head_sha, str):
        return ""
    return current_head_sha.strip()


def find_previous_reviewed_sha(issue_comments: Sequence[Mapping[str, object]]) -> str | None:
    for comment in reversed(issue_comments):
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        parsed_sha = parse_reviewed_head_sha(body)
        if parsed_sha:
            return parsed_sha
    return None


def build_review_resume_outputs(
    *,
    repository: str,
    pr_number: int | None,
    model_name: str,
    runner_temp: str,
    current_head_sha: str,
    previous_reviewed_sha: str | None,
) -> dict[str, str]:
    codex_home = str(Path(runner_temp or ".").resolve() / "codex-review-state")
    restore_key = ""
    current_cache_key = ""

    if pr_number and repository and model_name and current_head_sha:
        current_cache_key = compute_review_cache_key(
            repository,
            pr_number,
            model_name,
            current_head_sha,
        )
    if pr_number and repository and model_name and previous_reviewed_sha:
        restore_key = compute_review_cache_key(
            repository,
            pr_number,
            model_name,
            previous_reviewed_sha,
        )

    return {
        "codex_home": codex_home,
        "previous_reviewed_sha": previous_reviewed_sha or "",
        "restore_key": restore_key,
        "current_cache_key": current_cache_key,
    }


def load_latest_thread_id(codex_home: Path) -> str | None:
    session_index_path = codex_home / SESSION_INDEX_PATH
    lines: list[str] | None = None
    try:
        lines = session_index_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = None

    latest_thread_id = _load_latest_thread_id_from_index(lines)
    if latest_thread_id is not None:
        return latest_thread_id
    return _load_latest_thread_id_from_rollouts(codex_home)


def _load_latest_thread_id_from_index(lines: list[str] | None) -> str | None:
    if lines is None:
        return None
    latest_thread_id: str | None = None
    latest_updated_at = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        thread_id = payload.get("id")
        updated_at = payload.get("updated_at")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        if not isinstance(updated_at, str) or not updated_at:
            continue
        if updated_at >= latest_updated_at:
            latest_thread_id = thread_id
            latest_updated_at = updated_at
    return latest_thread_id


def _load_latest_thread_id_from_rollouts(codex_home: Path) -> str | None:
    rollouts_root = codex_home / ROLLOUTS_SUBDIR
    if not rollouts_root.is_dir():
        return None

    rollout_paths = sorted(
        rollouts_root.rglob("*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for rollout_path in rollout_paths:
        thread_id = _read_thread_id_from_rollout(rollout_path)
        if thread_id is not None:
            return thread_id
    return None


def _read_thread_id_from_rollout(rollout_path: Path) -> str | None:
    try:
        with rollout_path.open(encoding="utf-8") as handle:
            first_line = handle.readline().strip()
    except OSError:
        return None
    if not first_line:
        return None
    try:
        payload = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "session_meta":
        return None
    session_payload = payload.get("payload")
    if not isinstance(session_payload, dict):
        return None
    thread_id = session_payload.get("id")
    if not isinstance(thread_id, str):
        return None
    normalized = thread_id.strip()
    return normalized or None


def _sanitize_cache_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    sanitized = sanitized.strip("-")
    return sanitized or "unknown"
