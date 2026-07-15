from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from cli.clients import git_ops


def test_git_executable_requires_an_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_ops.shutil, "which", lambda name: "/usr/bin/git")  # noqa: ARG005
    assert git_ops._git_executable() == "/usr/bin/git"

    monkeypatch.setattr(git_ops.shutil, "which", lambda name: "git")  # noqa: ARG005
    with pytest.raises(RuntimeError, match="must be absolute"):
        git_ops._git_executable()

    monkeypatch.setattr(git_ops.shutil, "which", lambda name: None)  # noqa: ARG005
    with pytest.raises(RuntimeError, match="not found"):
        git_ops._git_executable()


def test_run_git_sets_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_ops, "_git_executable", lambda: "/usr/bin/git")
    call_args: dict[str, object] = {}

    def _fake_run(
        command: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        call_args.update(
            command=list(command),
            capture_output=capture_output,
            text=text,
            check=check,
            timeout=timeout,
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(git_ops.subprocess, "run", _fake_run)

    result = git_ops._run_git(["status", "--short"], capture_output=True)

    assert result.returncode == 0
    assert call_args == {
        "command": ["/usr/bin/git", "status", "--short"],
        "capture_output": True,
        "text": True,
        "check": False,
        "timeout": git_ops._GIT_COMMAND_TIMEOUT_SECONDS,
    }


def test_review_git_helpers_use_expected_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert text is True
        assert check is False
        calls.append(list(args))
        if args[0] == "merge-base":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, stdout="patch", stderr="")
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, stdout="a\nb\n", stderr="")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    assert git_ops.git_is_ancestor("old", "new") is True
    assert git_ops.git_diff_text("old..new", unified=1) == "patch"
    assert git_ops.git_commit_shas("old..new") == ["a", "b"]
    assert calls == [
        ["merge-base", "--is-ancestor", "old", "new"],
        ["diff", "--unified=1", "--no-color", "old..new"],
        ["rev-list", "--reverse", "old..new"],
    ]


@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
def test_git_is_ancestor_maps_expected_statuses(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        git_ops,
        "_run_git",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode),
    )

    assert git_ops.git_is_ancestor("old", "new") is expected


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: git_ops.git_is_ancestor("old", "new"), "merge-base"),
        (lambda: git_ops.git_diff_text("old..new"), "diff"),
        (lambda: git_ops.git_commit_shas("old..new"), "rev-list"),
    ],
)
def test_review_git_helpers_raise_on_command_failure(
    monkeypatch: pytest.MonkeyPatch,
    call,
    message: str,
) -> None:
    monkeypatch.setattr(
        git_ops,
        "_run_git",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            2,
            stdout="",
            stderr=message,
        ),
    )

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        call()
    assert exc_info.value.returncode == 2
    assert exc_info.value.stderr == message
