from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from codex.app_server import AppServerClient, AppServerProcessOptions, AppServerThreadListOptions
from codex.app_server.models import ThreadListResult
from codex.protocol import types as protocol

SUMMARY_METADATA_RE = re.compile(r"<!--\s*codex-review-meta\s+({.*?})\s*-->")
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


def load_latest_thread_id(codex_home: Path, cwd: Path) -> str | None:
    threads = _list_stored_threads(codex_home=codex_home, cwd=cwd)
    if not threads:
        return None
    latest_thread = max(threads, key=lambda thread: thread.updatedAt)
    return latest_thread.id


def _list_stored_threads(*, codex_home: Path, cwd: Path) -> list[protocol.Thread]:
    process_options = AppServerProcessOptions(env={"CODEX_HOME": str(codex_home)})
    list_options = AppServerThreadListOptions(cwd=str(cwd.resolve()))
    all_threads: list[protocol.Thread] = []

    try:
        with AppServerClient.connect_stdio(process_options) as client:
            cursor: str | None = None
            while True:
                page = client.list_threads_page(list_options.model_copy(update={"cursor": cursor}))
                all_threads.extend(page.data)
                cursor = _next_cursor(page)
                if cursor is None:
                    return all_threads
    except Exception:
        return []


def _next_cursor(page: ThreadListResult) -> str | None:
    next_cursor = page.next_cursor
    if not isinstance(next_cursor, str):
        return None
    normalized = next_cursor.strip()
    return normalized or None


def _sanitize_cache_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    sanitized = sanitized.strip("-")
    return sanitized or "unknown"
