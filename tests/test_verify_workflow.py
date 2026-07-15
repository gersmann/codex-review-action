from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from cli.core.config import ReviewConfig
from cli.core.exceptions import ConfigurationError
from cli.core.review_usage import ReviewUsage
from cli.workflows.verify_workflow import VerifyTarget, VerifyWorkflow


@dataclass
class _FakeUser:
    login: str = "octocat"


@dataclass
class _FakeRepoOwner:
    login: str = "owner"


@dataclass
class _FakeRepo:
    owner: _FakeRepoOwner = field(default_factory=_FakeRepoOwner)
    name: str = "repo"


@dataclass
class _FakeBase:
    ref: str = "main"
    sha: str = "base-sha"
    label: str = "owner:main"
    repo: _FakeRepo = field(default_factory=_FakeRepo)


class _FakeReviewComment:
    def __init__(
        self,
        body: str,
        *,
        comment_id: int,
        in_reply_to_id: int | None = None,
        path: str = "src.py",
        line: int = 3,
        login: str = "reviewer",
    ) -> None:
        self.id = comment_id
        self.body = body
        self.path = path
        self.line = line
        self.original_line = line
        self.in_reply_to_id = in_reply_to_id
        self.diff_hunk = "@@ -1 +1 @@"
        self.commit_id = "head-sha"
        self.user = _FakeUser(login)
        self.created_at = "now"


class _FakePR:
    def __init__(self, *, review_comments: list[_FakeReviewComment] | None = None) -> None:
        self.number = 7
        self.title = "Improve review flow"
        self.base = _FakeBase()
        self._review_comments = review_comments or []

    def get_review_comments(self) -> list[_FakeReviewComment]:
        return list(self._review_comments)


class _FakeGitHubClient:
    def __init__(self, pr: _FakePR) -> None:
        self.pr = pr
        self.calls: list[int] = []
        self.replies: list[tuple[int, str]] = []
        self.issue_comments: list[str] = []

    def get_pr(self, pr_number: int) -> _FakePR:
        self.calls.append(pr_number)
        return self.pr

    def reply_to_review_comment(self, pr: _FakePR, comment_id: int, text: str) -> int:
        assert pr is self.pr
        self.replies.append((comment_id, text))
        return 1000 + len(self.replies)

    def post_issue_comment(self, pr: _FakePR, text: str) -> int:
        assert pr is self.pr
        self.issue_comments.append(text)
        return 2000 + len(self.issue_comments)


class _FakeCodexClient:
    def __init__(self, response: str, *, usage: ReviewUsage | None = None) -> None:
        self.response = response
        self.usage = usage
        self.calls: list[dict[str, Any]] = []

    def execute_structured(
        self,
        prompt: str,
        *,
        output_schema: dict[str, object],
        schema_prompt: str,
        sandbox_mode: str,
        resume_thread_id: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "output_schema": output_schema,
                "schema_prompt": schema_prompt,
                "sandbox_mode": sandbox_mode,
                "resume_thread_id": resume_thread_id,
            }
        )
        return self.response


def _make_config(tmp_path: Path, *, dry_run: bool = False) -> ReviewConfig:
    return ReviewConfig(
        github_token="token",
        repository="owner/repo",
        pr_number=7,
        dry_run=dry_run,
        repo_root=tmp_path,
    )


def _verify_response(
    *,
    verdict: str = "correct",
    explanation: str = "Loop increments before the call.",
    confidence_score: float | None = 0.9,
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "explanation": explanation,
            "confidence_score": confidence_score,
        }
    )


def _freeze_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module

    ticks = iter([100.0])
    monkeypatch.setattr(time_module, "monotonic", lambda: next(ticks, 372.0))


def test_process_verify_replies_in_thread_with_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pr = _FakePR(
        review_comments=[
            _FakeReviewComment("the retry loop is off by one", comment_id=41),
            _FakeReviewComment("Are you sure?", comment_id=42, in_reply_to_id=41),
            _FakeReviewComment("/verify", comment_id=43, in_reply_to_id=41),
            _FakeReviewComment("unrelated thread", comment_id=50),
        ]
    )
    github_client = _FakeGitHubClient(pr)
    codex_client = _FakeCodexClient(_verify_response())
    workflow = VerifyWorkflow(
        _make_config(tmp_path),
        github_client=cast(Any, github_client),
        codex_client=cast(Any, codex_client),
    )
    _freeze_monotonic(monkeypatch)

    result = workflow.process_verify(
        7,
        VerifyTarget(claim="", thread_root_id=41, path="src.py", line=3),
    )

    assert result.verify.verdict == "correct"
    assert github_client.issue_comments == []
    assert len(github_client.replies) == 1
    comment_id, body = github_client.replies[0]
    assert comment_id == 41
    assert "### Codex Verification" in body
    assert "**Verdict:** the claim is correct." in body
    assert "Loop increments before the call." in body
    assert "Confidence: 0.90" in body
    assert "### Usage" in body
    assert "- Time elapsed: 4m 32s" in body

    assert codex_client.calls[0]["sandbox_mode"] == "danger-full-access"
    assert codex_client.calls[0]["schema_prompt"] == "Produce the JSON verify output now."
    assert codex_client.calls[0]["resume_thread_id"] is None
    prompt = codex_client.calls[0]["prompt"]
    assert "<review_thread>" in prompt
    assert "<location>src.py:3</location>" in prompt
    assert "the retry loop is off by one" in prompt
    assert "Are you sure?" in prompt
    assert "unrelated thread" not in prompt
    assert "/verify" not in prompt


def test_process_verify_issue_claim_posts_issue_comment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pr = _FakePR()
    github_client = _FakeGitHubClient(pr)
    codex_client = _FakeCodexClient(
        _verify_response(
            verdict="uncertain",
            explanation="Cache key derivation is not in this repository.",
            confidence_score=None,
        )
    )
    workflow = VerifyWorkflow(
        _make_config(tmp_path),
        github_client=cast(Any, github_client),
        codex_client=cast(Any, codex_client),
    )
    _freeze_monotonic(monkeypatch)

    result = workflow.process_verify(
        7,
        VerifyTarget(claim="is the cache key stable?", thread_root_id=None),
    )

    assert result.verify.verdict == "uncertain"
    assert github_client.replies == []
    assert len(github_client.issue_comments) == 1
    body = github_client.issue_comments[0]
    assert "**Verdict:** the claim could not be confirmed from this repository." in body
    assert "Cache key derivation is not in this repository." in body
    assert "Confidence:" not in body
    assert "### Usage" in body
    assert "- Time elapsed:" in body

    prompt = codex_client.calls[0]["prompt"]
    assert "<claim>\nis the cache key stable?\n</claim>" in prompt
    assert "<review_thread>" not in prompt


def test_process_verify_location_omits_line_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pr = _FakePR(
        review_comments=[
            _FakeReviewComment("the retry loop is off by one", comment_id=41),
        ]
    )
    github_client = _FakeGitHubClient(pr)
    codex_client = _FakeCodexClient(_verify_response())
    workflow = VerifyWorkflow(
        _make_config(tmp_path),
        github_client=cast(Any, github_client),
        codex_client=cast(Any, codex_client),
    )
    _freeze_monotonic(monkeypatch)

    workflow.process_verify(
        7,
        VerifyTarget(claim="", thread_root_id=41, path="src.py", line=None),
    )

    prompt = codex_client.calls[0]["prompt"]
    assert "<location>src.py</location>" in prompt
    assert "<location>src.py:" not in prompt


def test_process_verify_filters_commands_but_keeps_mentions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pr = _FakePR(
        review_comments=[
            _FakeReviewComment("the retry loop is off by one", comment_id=41),
            _FakeReviewComment("/review", comment_id=42, in_reply_to_id=41),
            _FakeReviewComment(
                "/reviewers please note the boundary case",
                comment_id=44,
                in_reply_to_id=41,
            ),
            _FakeReviewComment(
                "I ran /review earlier and it flagged this too",
                comment_id=45,
                in_reply_to_id=41,
            ),
            _FakeReviewComment(
                "### Codex Verification\n**Verdict:** the claim is correct.",
                comment_id=46,
                in_reply_to_id=41,
            ),
            _FakeReviewComment("/verify", comment_id=47, in_reply_to_id=41),
        ]
    )
    github_client = _FakeGitHubClient(pr)
    codex_client = _FakeCodexClient(_verify_response())
    workflow = VerifyWorkflow(
        _make_config(tmp_path),
        github_client=cast(Any, github_client),
        codex_client=cast(Any, codex_client),
    )
    _freeze_monotonic(monkeypatch)

    workflow.process_verify(
        7,
        VerifyTarget(claim="", thread_root_id=41, path="src.py", line=3),
    )

    prompt = codex_client.calls[0]["prompt"]
    assert "/reviewers please note the boundary case" in prompt
    assert "I ran /review earlier and it flagged this too" in prompt
    assert "**Verdict:** the claim is correct." not in prompt
    assert '"body": "/review"' not in prompt
    assert '"body": "/verify"' not in prompt


def test_process_verify_filters_queued_acks_from_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pr = _FakePR(
        review_comments=[
            _FakeReviewComment("the retry loop is off by one", comment_id=41),
            _FakeReviewComment(
                "@robcole, your Codex verification has been queued.",
                comment_id=42,
                in_reply_to_id=41,
                login="github-actions[bot]",
            ),
            _FakeReviewComment(
                "@robcole, your Codex review has been queued.",
                comment_id=43,
                in_reply_to_id=41,
                login="github-actions[bot]",
            ),
            _FakeReviewComment(
                "I asked about your Codex settings; the queue has been queued.",
                comment_id=44,
                in_reply_to_id=41,
            ),
        ]
    )
    github_client = _FakeGitHubClient(pr)
    codex_client = _FakeCodexClient(_verify_response())
    workflow = VerifyWorkflow(
        _make_config(tmp_path),
        github_client=cast(Any, github_client),
        codex_client=cast(Any, codex_client),
    )
    _freeze_monotonic(monkeypatch)

    workflow.process_verify(
        7,
        VerifyTarget(claim="", thread_root_id=41, path="src.py", line=3),
    )

    prompt = codex_client.calls[0]["prompt"]
    assert "the retry loop is off by one" in prompt
    assert "I asked about your Codex settings; the queue has been queued." in prompt
    assert "your Codex verification has been queued." not in prompt
    assert "your Codex review has been queued." not in prompt


def test_process_verify_empty_thread_without_claim_raises(tmp_path: Path) -> None:
    pr = _FakePR(
        review_comments=[
            _FakeReviewComment("/verify", comment_id=41),
        ]
    )
    github_client = _FakeGitHubClient(pr)
    codex_client = _FakeCodexClient(_verify_response())
    workflow = VerifyWorkflow(
        _make_config(tmp_path),
        github_client=cast(Any, github_client),
        codex_client=cast(Any, codex_client),
    )

    with pytest.raises(ConfigurationError, match="Nothing to verify"):
        workflow.process_verify(
            7,
            VerifyTarget(claim="", thread_root_id=41, path="src.py", line=3),
        )

    assert codex_client.calls == []
    assert github_client.replies == []
    assert github_client.issue_comments == []


def test_process_verify_requires_thread_or_claim(tmp_path: Path) -> None:
    pr = _FakePR()
    github_client = _FakeGitHubClient(pr)
    codex_client = _FakeCodexClient(_verify_response())
    workflow = VerifyWorkflow(
        _make_config(tmp_path),
        github_client=cast(Any, github_client),
        codex_client=cast(Any, codex_client),
    )

    with pytest.raises(ConfigurationError, match="Nothing to verify"):
        workflow.process_verify(7, VerifyTarget(claim="   ", thread_root_id=None))

    assert github_client.calls == []
    assert codex_client.calls == []


def test_process_verify_dry_run_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pr = _FakePR(
        review_comments=[
            _FakeReviewComment("the retry loop is off by one", comment_id=41),
        ]
    )
    github_client = _FakeGitHubClient(pr)
    codex_client = _FakeCodexClient(_verify_response(verdict="incorrect"))
    workflow = VerifyWorkflow(
        _make_config(tmp_path, dry_run=True),
        github_client=cast(Any, github_client),
        codex_client=cast(Any, codex_client),
    )
    _freeze_monotonic(monkeypatch)

    result = workflow.process_verify(
        7,
        VerifyTarget(claim="", thread_root_id=41, path="src.py", line=3),
    )

    assert result.verify.verdict == "incorrect"
    assert github_client.replies == []
    assert github_client.issue_comments == []
    assert len(codex_client.calls) == 1
