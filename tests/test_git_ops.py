from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from cli.clients.git_ops import (
    GitWorktreeSnapshot,
    git_changed_paths_since_snapshot,
    git_commit_shas,
    git_diff_text,
    git_format_called_process_error,
)


def test_git_ops_helpers_cover_snapshot_diff_and_error_formatting() -> None:
    before = GitWorktreeSnapshot(
        changed_paths=frozenset({"a.py", "b.py"}),
        path_states={"a.py": (True, "1"), "b.py": (True, "2")},
    )
    after = GitWorktreeSnapshot(
        changed_paths=frozenset({"b.py", "c.py"}),
        path_states={"b.py": (True, "3"), "c.py": (False, None)},
    )
    assert git_changed_paths_since_snapshot(before, after) == ["b.py", "c.py"]

    exc = subprocess.CalledProcessError(
        2,
        ["git", "push"],
        output="line1\nline2\n",
        stderr="err1\nerr2\n",
    )
    formatted = git_format_called_process_error(exc)
    assert "command `git push` exited with code 2" in formatted
    assert "stderr:\nerr1\nerr2" in formatted


def test_git_executable_re_resolves_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.clients import git_ops

    monkeypatch.setattr(git_ops.shutil, "which", lambda name: "/usr/bin/git")  # noqa: ARG005
    assert git_ops._git_executable() == "/usr/bin/git"

    monkeypatch.setattr(git_ops.shutil, "which", lambda name: "git")  # noqa: ARG005
    with pytest.raises(RuntimeError, match="must be absolute"):
        git_ops._git_executable()

    monkeypatch.setattr(git_ops.shutil, "which", lambda name: None)  # noqa: ARG005
    with pytest.raises(RuntimeError, match="not found"):
        git_ops._git_executable()


def test_run_git_sets_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.clients import git_ops

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
        call_args["command"] = list(command)
        call_args["capture_output"] = capture_output
        call_args["text"] = text
        call_args["check"] = check
        call_args["timeout"] = timeout
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


def test_git_head_is_ahead_returns_false_for_unknown_remote_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

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
        if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    assert git_ops.git_head_is_ahead(None) is False


def test_git_head_is_ahead_uses_upstream_branch_when_branch_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return subprocess.CompletedProcess(args, 0, stdout="origin/feature\n", stderr="")
        if args[:2] == ["ls-remote", "--exit-code"]:
            assert args[-2:] == ["origin", "feature"]
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="deadbeef\trefs/heads/feature\n",
                stderr="",
            )
        if args[:2] == ["rev-list", "--left-right"]:
            assert args[-1] == "HEAD...origin/feature"
            return subprocess.CompletedProcess(args, 0, stdout="1\t0\n", stderr="")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    assert git_ops.git_head_is_ahead(None) is True
    assert any(args[:2] == ["ls-remote", "--exit-code"] for args in calls)


def test_git_head_is_ahead_raises_when_remote_probe_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

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
        if args[:2] == ["ls-remote", "--exit-code"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="network down")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError):
        git_ops.git_head_is_ahead("feature")


def test_git_diff_text_and_commit_shas_use_expected_git_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        if args[:3] == ["diff", "--unified=3", "--no-color"]:
            return subprocess.CompletedProcess(args, 0, stdout="diff text\n", stderr="")
        if args[:2] == ["rev-list", "--reverse"]:
            return subprocess.CompletedProcess(args, 0, stdout="a\nb\n", stderr="")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    assert git_diff_text("prev..HEAD") == "diff text\n"
    assert git_commit_shas("prev..HEAD") == ["a", "b"]
    assert calls == [
        ["diff", "--unified=3", "--no-color", "prev..HEAD"],
        ["rev-list", "--reverse", "prev..HEAD"],
    ]


def test_git_commit_paths_commits_when_staged_changes_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[tuple[Sequence[str], bool]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,  # noqa: ARG001
        text: bool = True,  # noqa: ARG001
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, check))
        if args[:2] == ["add", "--"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        if args[:1] == ["commit"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    assert git_ops.git_commit_paths("Codex edit: test", ["a.py"]) is True
    assert calls == [
        (["add", "--", "a.py"], True),
        (["diff", "--cached", "--quiet"], False),
        (["commit", "-m", "Codex edit: test"], True),
    ]


def test_git_commit_paths_raises_when_staged_check_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,  # noqa: ARG001
        text: bool = True,  # noqa: ARG001
        check: bool = False,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["add", "--"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(args, 2, stdout="", stderr="fatal")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_commit_paths("Codex edit: test", ["a.py"])
    assert exc_info.value.returncode == 2
    assert exc_info.value.stderr == "fatal"


def test_git_rebase_in_progress_checks_rebase_head_then_git_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cli.clients import git_ops

    git_dir = tmp_path / ".git"
    (git_dir / "rebase-merge").mkdir(parents=True)

    calls: list[Sequence[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        if args == ["rev-parse", "--verify", "REBASE_HEAD"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        if args == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args, 0, stdout=str(git_dir), stderr="")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    assert git_ops.git_rebase_in_progress() is True
    assert calls == [
        ["rev-parse", "--verify", "REBASE_HEAD"],
        ["rev-parse", "--git-dir"],
    ]


def test_git_current_head_sha_raises_when_rev_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

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
        if args == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: no HEAD")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_current_head_sha()
    assert exc_info.value.returncode == 128


def test_git_has_changes_raises_when_status_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

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
        if args == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: no repo")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_has_changes()
    assert exc_info.value.returncode == 128


def test_git_rebase_in_progress_raises_when_git_dir_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

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
        if args == ["rev-parse", "--verify", "REBASE_HEAD"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        if args == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: no repo")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_rebase_in_progress()
    assert exc_info.value.returncode == 128


def test_git_push_head_to_branch_succeeds_without_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    git_ops.git_push_head_to_branch("feature", lambda level, message: None)  # noqa: ARG005

    assert calls == [["push", "origin", "HEAD:refs/heads/feature"]]


def test_git_push_force_with_lease_raises_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="stale info")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_push_force_with_lease("feature", "abc123")
    assert exc_info.value.stderr == "stale info"
    assert calls == [
        [
            "push",
            "origin",
            "HEAD:refs/heads/feature",
            "--force-with-lease",
            "--force-with-lease=refs/heads/feature:abc123",
        ]
    ]


def test_git_push_force_with_lease_succeeds_on_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    git_ops.git_push_force_with_lease("feature", None)
    assert calls == [["push", "origin", "HEAD:refs/heads/feature", "--force-with-lease"]]


def test_git_push_head_to_branch_raises_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []
    debug_messages: list[str] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        if args[:2] == ["push", "origin"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="non-fast-forward")
        if args[:2] == ["fetch", "origin"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="network down")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_push_head_to_branch(
            "feature",
            lambda level, message: debug_messages.append(f"{level}:{message}"),
        )
    assert exc_info.value.stderr == "network down"

    assert any("Push rejected for feature" in message for message in debug_messages)
    assert calls == [
        ["push", "origin", "HEAD:refs/heads/feature"],
        ["fetch", "origin", "feature"],
    ]


def test_git_push_head_to_branch_aborts_rebase_on_rebase_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert text is True
        assert check is False
        if args[:2] == ["push", "origin"]:
            assert capture_output is True
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="non-fast-forward")
        if args[:2] == ["fetch", "origin"]:
            assert capture_output is True
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:1] == ["rebase"] and args[-1] != "--abort":
            assert capture_output is True
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="conflict")
        if args == ["rebase", "--abort"]:
            assert capture_output is False
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_push_head_to_branch("feature", lambda level, message: None)  # noqa: ARG005
    assert exc_info.value.stderr == "conflict"

    assert calls == [
        ["push", "origin", "HEAD:refs/heads/feature"],
        ["fetch", "origin", "feature"],
        ["rebase", "origin/feature"],
        ["rebase", "--abort"],
    ]


def test_git_push_head_to_branch_raises_when_final_push_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        if args[:2] == ["push", "origin"] and len(calls) == 1:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="non-fast-forward")
        if args[:2] == ["fetch", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:1] == ["rebase"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["push", "origin"] and len(calls) == 4:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="still rejected")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_push_head_to_branch("feature", lambda level, message: None)  # noqa: ARG005
    assert exc_info.value.stderr == "still rejected"

    assert calls == [
        ["push", "origin", "HEAD:refs/heads/feature"],
        ["fetch", "origin", "feature"],
        ["rebase", "origin/feature"],
        ["push", "origin", "HEAD:refs/heads/feature"],
    ]


def test_git_push_head_to_branch_raises_without_retry_for_non_rebaseable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

    calls: list[Sequence[str]] = []
    debug_messages: list[str] = []

    def _fake_run_git(
        args: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert check is False
        if args[:2] == ["push", "origin"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="permission denied")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_push_head_to_branch(
            "feature",
            lambda level, message: debug_messages.append(f"{level}:{message}"),
        )
    assert exc_info.value.stderr == "permission denied"
    assert calls == [["push", "origin", "HEAD:refs/heads/feature"]]
    assert debug_messages == []


def test_git_worktree_snapshot_raises_when_changed_path_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.clients import git_ops

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
        if args == ["diff", "--name-only", "--"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: no repo")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(git_ops, "_run_git", _fake_run_git)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        git_ops.git_worktree_snapshot()
    assert exc_info.value.returncode == 128
    assert exc_info.value.stderr == "fatal: no repo"
