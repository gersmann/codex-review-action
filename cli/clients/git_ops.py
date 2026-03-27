from __future__ import annotations

import hashlib
import shutil
import subprocess  # nosec B404
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_GIT_COMMAND_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class GitWorktreeSnapshot:
    """Snapshot of dirty paths and their content state."""

    changed_paths: frozenset[str]
    path_states: dict[str, tuple[bool, str | None]]


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
    """Run a git command through a single validated subprocess boundary.

    This wrapper only executes the `git` binary with argument vectors and never
    uses a shell. Callers pass git subcommands and refs, not arbitrary shell
    fragments.
    """
    command = [_git_executable(), *args]
    return subprocess.run(  # nosec B603
        command,
        capture_output=capture_output,
        text=text,
        check=check,
        timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
    )


def _raise_git_result_error(result: subprocess.CompletedProcess[str]) -> None:
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        result.stdout,
        result.stderr,
    )


def git_has_changes() -> bool:
    result = _run_git(["status", "--porcelain"], capture_output=True)
    if result.returncode != 0:
        _raise_git_result_error(result)
    return bool(result.stdout.strip())


def git_status_pretty() -> None:
    _run_git(["status", "--short"])


def git_setup_identity() -> None:
    _run_git(
        [
            "config",
            "user.email",
            "github-actions[bot]@users.noreply.github.com",
        ],
        check=True,
    )
    _run_git(["config", "user.name", "github-actions[bot]"], check=True)


def git_commit_paths(message: str, paths: Sequence[str]) -> bool:
    """Stage selected paths and commit, returning True when a commit was created."""
    normalized_paths = [path for path in paths if path]
    if not normalized_paths:
        return False

    _run_git(["add", "--", *normalized_paths], check=True)
    staged_check = _run_git(["diff", "--cached", "--quiet"])
    if staged_check.returncode == 0:
        return False
    if staged_check.returncode > 1:
        raise subprocess.CalledProcessError(
            staged_check.returncode,
            staged_check.args,
            staged_check.stdout,
            staged_check.stderr,
        )

    _run_git(["commit", "-m", message], check=True)
    return True


def git_worktree_snapshot() -> GitWorktreeSnapshot:
    """Capture current dirty paths and their file-content state."""
    changed_paths = _collect_changed_paths()
    states: dict[str, tuple[bool, str | None]] = {}
    for path in changed_paths:
        states[path] = _path_state(path)
    return GitWorktreeSnapshot(changed_paths=frozenset(changed_paths), path_states=states)


def git_changed_paths_since_snapshot(
    before: GitWorktreeSnapshot,
    after: GitWorktreeSnapshot,
) -> list[str]:
    """Return changed paths attributable to activity between two snapshots."""
    touched: set[str] = set(after.changed_paths - before.changed_paths)

    for path in before.changed_paths & after.changed_paths:
        if before.path_states.get(path) != after.path_states.get(path):
            touched.add(path)

    return sorted(touched)


def git_current_head_sha() -> str | None:
    """Return current HEAD SHA.

    Returns:
        SHA string for HEAD, or ``None`` when git returned an empty value.

    Raises:
        subprocess.CalledProcessError: Git probe failed.
    """
    result = _run_git(["rev-parse", "HEAD"], capture_output=True)
    if result.returncode != 0:
        _raise_git_result_error(result)
    sha = result.stdout.strip()
    return sha or None


def git_remote_head_sha(branch: str | None, *, remote: str = "origin") -> str | None:
    """Return remote head SHA for a branch.

    Returns:
        SHA string for the remote branch, or ``None`` when branch/remote input is empty
        or the branch does not exist on the remote.

    Raises:
        subprocess.CalledProcessError: Git probe failed for reasons other than a missing branch.
    """
    if not branch or not remote:
        return None
    result = _run_git(["ls-remote", "--exit-code", "--heads", remote, branch], capture_output=True)
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        _raise_git_result_error(result)
    first_line = (result.stdout.strip().splitlines() or [""])[0]
    if not first_line:
        return None
    sha = first_line.split()[0] if first_line.split() else ""
    return sha or None


def git_is_ancestor(older_sha: str, newer_sha: str) -> bool:
    """Return whether ``older_sha`` is an ancestor of ``newer_sha``.

    Raises:
        subprocess.CalledProcessError: Git probe failed.
    """
    result = _run_git(
        ["merge-base", "--is-ancestor", older_sha, newer_sha],
        capture_output=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        result.stdout,
        result.stderr,
    )


def git_diff_text(revision_range: str, *, unified: int = 3) -> str:
    """Return the git diff for ``revision_range``.

    Raises:
        subprocess.CalledProcessError: Git diff failed.
    """
    result = _run_git(
        ["diff", f"--unified={unified}", "--no-color", revision_range],
        capture_output=True,
    )
    if result.returncode != 0:
        _raise_git_result_error(result)
    return result.stdout


def git_commit_shas(revision_range: str) -> list[str]:
    """Return commit SHAs in ``revision_range`` from oldest to newest.

    Raises:
        subprocess.CalledProcessError: Git log probe failed.
    """
    result = _run_git(
        ["rev-list", "--reverse", revision_range],
        capture_output=True,
    )
    if result.returncode != 0:
        _raise_git_result_error(result)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_rebase_in_progress() -> bool:
    """Return whether repository is in an active rebase state.

    Raises:
        subprocess.CalledProcessError: Git probe failed.
    """
    probe = _run_git(["rev-parse", "--verify", "REBASE_HEAD"], capture_output=True)
    if probe.returncode == 0:
        return True

    git_dir_result = _run_git(["rev-parse", "--git-dir"], capture_output=True)
    if git_dir_result.returncode != 0:
        _raise_git_result_error(git_dir_result)
    git_dir = Path(git_dir_result.stdout.strip())
    return (git_dir / "rebase-apply").exists() or (git_dir / "rebase-merge").exists()


def git_push_force_with_lease(branch: str, expected_remote_sha: str | None) -> None:
    """Push rewritten history safely with force-with-lease protection."""
    command = [
        "push",
        "origin",
        f"HEAD:refs/heads/{branch}",
        "--force-with-lease",
    ]
    if expected_remote_sha:
        command.append(f"--force-with-lease=refs/heads/{branch}:{expected_remote_sha}")
    result = _run_git(command, capture_output=True)
    if result.returncode != 0:
        _raise_git_result_error(result)


def git_format_called_process_error(exc: subprocess.CalledProcessError, max_lines: int = 12) -> str:
    """Format subprocess git errors with command and output snippets."""
    cmd = exc.cmd
    if isinstance(cmd, (list, tuple)):
        command_text = " ".join(str(part) for part in cmd)
    else:
        command_text = str(cmd)

    stdout_text = exc.stdout if isinstance(exc.stdout, str) else ""
    stderr_text = exc.stderr if isinstance(exc.stderr, str) else ""
    stdout = stdout_text.strip()
    stderr = stderr_text.strip()
    lines: list[str] = [f"command `{command_text}` exited with code {exc.returncode}"]

    if stderr:
        stderr_tail = "\n".join(stderr.splitlines()[-max_lines:])
        lines.append(f"stderr:\n{stderr_tail}")
    if stdout:
        stdout_tail = "\n".join(stdout.splitlines()[-max_lines:])
        lines.append(f"stdout:\n{stdout_tail}")
    return "\n".join(lines)


def git_push() -> None:
    """Simple git push to the current tracking branch."""
    _run_git(["push"], check=True)


def git_push_head_to_branch(branch: str, debug: Callable[[int, str], None]) -> None:
    """Push HEAD to branch, retrying once with fetch/rebase if needed."""
    push_cmd = ["push", "origin", f"HEAD:refs/heads/{branch}"]
    push_result = _run_git(push_cmd, capture_output=True)
    if push_result.returncode == 0:
        return

    if not _is_non_fast_forward_push_rejection(push_result):
        raise subprocess.CalledProcessError(
            push_result.returncode,
            push_result.args,
            push_result.stdout,
            push_result.stderr,
        )

    debug(1, f"Push rejected for {branch}; attempting fetch/rebase.")

    fetch_result = _run_git(["fetch", "origin", branch], capture_output=True)
    if fetch_result.returncode != 0:
        raise subprocess.CalledProcessError(
            fetch_result.returncode,
            fetch_result.args,
            fetch_result.stdout,
            fetch_result.stderr or push_result.stderr,
        )

    rebase_target = f"origin/{branch}"
    rebase_result = _run_git(["rebase", rebase_target], capture_output=True)
    if rebase_result.returncode != 0:
        _run_git(["rebase", "--abort"])
        raise subprocess.CalledProcessError(
            rebase_result.returncode,
            rebase_result.args,
            rebase_result.stdout,
            rebase_result.stderr,
        )

    final_push_result = _run_git(push_cmd, capture_output=True)
    if final_push_result.returncode != 0:
        raise subprocess.CalledProcessError(
            final_push_result.returncode,
            final_push_result.args,
            final_push_result.stdout,
            final_push_result.stderr,
        )


def _is_non_fast_forward_push_rejection(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    stderr = result.stderr.lower() if isinstance(result.stderr, str) else ""
    stdout = result.stdout.lower() if isinstance(result.stdout, str) else ""
    text = "\n".join([stderr, stdout])
    return any(
        marker in text
        for marker in (
            "non-fast-forward",
            "fetch first",
            "[rejected]",
            "failed to push some refs",
        )
    )


def git_head_is_ahead(branch: str | None) -> bool:
    """Return True if HEAD has commits that are not on the remote branch."""
    ref = None
    remote = "origin"
    remote_branch = branch
    if branch:
        ref = f"origin/{branch}"
    else:
        upstream_result = _run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True,
        )
        if upstream_result.returncode != 0:
            return False

        ref = upstream_result.stdout.strip()
        if not ref:
            return False

        remote, _, remote_branch = ref.partition("/")
        if not remote or not remote_branch:
            return False

    if not ref:
        return False

    remote_head = git_remote_head_sha(remote_branch, remote=remote)
    if remote_head is None:
        return False

    compare_result = _run_git(
        ["rev-list", "--left-right", "--count", f"HEAD...{ref}"],
        capture_output=True,
    )
    if compare_result.returncode != 0:
        _raise_git_result_error(compare_result)

    parts = (compare_result.stdout.strip() or "0\t0").split()
    try:
        ahead = int(parts[0]) if parts else 0
    except ValueError:
        raise RuntimeError(f"unexpected rev-list count output: {compare_result.stdout!r}") from None
    return ahead > 0


def _collect_changed_paths() -> set[str]:
    paths: set[str] = set()
    commands = [
        ["diff", "--name-only", "--"],
        ["diff", "--cached", "--name-only", "--"],
        ["ls-files", "--others", "--exclude-standard"],
    ]

    for command in commands:
        result = _run_git(command, capture_output=True)
        if result.returncode != 0:
            _raise_git_result_error(result)
        for line in result.stdout.splitlines():
            path = line.strip()
            if path:
                paths.add(path)
    return paths


def _path_state(path: str) -> tuple[bool, str | None]:
    file_path = Path(path)
    if not file_path.exists():
        return (False, None)

    if not file_path.is_file():
        return (True, None)

    try:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return (True, None)
    return (True, digest)
