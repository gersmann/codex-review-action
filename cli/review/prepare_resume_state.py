from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..core.config import ReviewConfig
from .dedupe import SUMMARY_MARKER
from .resume_state import (
    build_review_resume_outputs,
    extract_current_head_sha,
    find_previous_reviewed_sha,
)


def _load_event(event_path: str) -> dict[str, Any]:
    with open(event_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("GitHub event payload must be an object")
    return payload


def _fetch_issue_comments(
    *,
    api_url: str,
    repository: str,
    pr_number: int,
    github_token: str,
) -> list[dict[str, object]]:
    issue_comments: list[dict[str, object]] = []
    page = 1
    while True:
        url = f"{api_url}/repos/{repository}/issues/{pr_number}/comments?per_page=100&page={page}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        if not isinstance(payload, list) or not payload:
            break
        issue_comments.extend(comment for comment in payload if isinstance(comment, dict))
        if len(payload) < 100:
            break
        page += 1
    return issue_comments


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    github_output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not event_path or not github_output:
        raise RuntimeError("GITHUB_EVENT_PATH and GITHUB_OUTPUT are required")

    event = _load_event(event_path)
    pr_number = ReviewConfig.extract_pr_number_from_event(event)
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    model_name = os.environ.get("CODEX_MODEL_INPUT", "").strip()
    runner_temp = os.environ.get("RUNNER_TEMP", "").strip()
    current_head_sha = extract_current_head_sha(event)

    previous_reviewed_sha = None
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    api_url = os.environ.get("GITHUB_API_URL", "").strip()
    if pr_number and repository and github_token and api_url:
        try:
            issue_comments = _fetch_issue_comments(
                api_url=api_url,
                repository=repository,
                pr_number=pr_number,
                github_token=github_token,
            )
        except urllib.error.URLError as exc:
            print(f"warning: failed to fetch prior review summaries: {exc}", file=sys.stderr)
        else:
            summary_comments: list[dict[str, object]] = []
            for comment in issue_comments:
                body = comment.get("body")
                if isinstance(body, str) and SUMMARY_MARKER in body:
                    summary_comments.append(comment)
            previous_reviewed_sha = find_previous_reviewed_sha(summary_comments)

    outputs = build_review_resume_outputs(
        repository=repository,
        pr_number=pr_number,
        model_name=model_name,
        runner_temp=runner_temp,
        current_head_sha=current_head_sha,
        previous_reviewed_sha=previous_reviewed_sha,
    )
    Path(outputs["codex_home"]).mkdir(parents=True, exist_ok=True)

    output_path = Path(github_output)
    with output_path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
