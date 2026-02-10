from __future__ import annotations

import json
import sys

from cli import main as main_module


def test_main_noops_for_non_codex_comment_event(monkeypatch, tmp_path) -> None:
    event_payload = {
        "issue": {"number": 17, "pull_request": {"url": "https://example.test/pr/17"}},
        "comment": {
            "id": 123,
            "body": "looks good to me",
            "user": {"login": "octocat"},
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")

    class _UnexpectedWorkflow:
        def __init__(self, config):  # noqa: ARG002
            raise AssertionError("workflow must not be instantiated for non-command comment")

    monkeypatch.setattr(main_module, "ReviewWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(main_module, "EditWorkflow", _UnexpectedWorkflow)
    monkeypatch.setattr(sys, "argv", ["codex-review"])

    rc = main_module.main()

    assert rc == 0
