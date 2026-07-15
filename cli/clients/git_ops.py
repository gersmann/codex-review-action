from __future__ import annotations

import shutil
import subprocess  # nosec B404
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

_GIT_COMMAND_TIMEOUT_SECONDS = 120


def _git_executable() -> str:
    git_executable = shutil.which("git")
    if not git_executable:
        raise RuntimeError("git executable not found on PATH")

    git_path = Path(git_executable)
    if not git_path.is_absolute():
        raise RuntimeError(f"git executable path must be absolute, got: {git_executable}")
    return str(git_path)


def _run_git(
    args: Sequence[str],
    *,
    capture_output: bool = False,
    text: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a git command through a single validated subprocess boundary."""
    command = [_git_executable(), *args]
    return subprocess.run(  # nosec B603
        command,
        capture_output=capture_output,
        text=text,
        check=check,
        timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
    )


def _raise_git_result_error(result: subprocess.CompletedProcess[str]) -> NoReturn:
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        result.stdout,
        result.stderr,
    )


def git_is_ancestor(older_sha: str, newer_sha: str) -> bool:
    """Return whether ``older_sha`` is an ancestor of ``newer_sha``."""
    result = _run_git(
        ["merge-base", "--is-ancestor", older_sha, newer_sha],
        capture_output=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    _raise_git_result_error(result)


def git_diff_text(revision_range: str, *, unified: int = 3) -> str:
    """Return the git diff for ``revision_range``."""
    result = _run_git(
        ["diff", f"--unified={unified}", "--no-color", revision_range],
        capture_output=True,
    )
    if result.returncode != 0:
        _raise_git_result_error(result)
    return result.stdout


def git_commit_shas(revision_range: str) -> list[str]:
    """Return commit SHAs in ``revision_range`` from oldest to newest."""
    result = _run_git(
        ["rev-list", "--reverse", revision_range],
        capture_output=True,
    )
    if result.returncode != 0:
        _raise_git_result_error(result)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
