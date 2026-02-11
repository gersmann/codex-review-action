from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitWorktreeSnapshot:
    """Snapshot of dirty paths and their content state."""

    changed_paths: frozenset[str]
    path_states: dict[str, tuple[bool, str | None]]


@dataclass(frozen=True)
class GitPushResult:
    """Captured result of a push command."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def git_has_changes() -> bool:
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    return bool(result.stdout.strip())


def git_status_pretty() -> None:
    subprocess.run(["git", "status", "--short"], check=False)


def git_setup_identity() -> None:
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "github-actions[bot]@users.noreply.github.com",
        ],
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)


def git_commit_paths(message: str, paths: Sequence[str]) -> bool:
    """Stage selected paths and commit, returning True when a commit was created."""
    normalized_paths = [path for path in paths if path]
    if not normalized_paths:
        return False

    subprocess.run(["git", "add", "--", *normalized_paths], check=True)
    staged_check = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if staged_check.returncode == 0:
        return False
    if staged_check.returncode > 1:
        raise subprocess.CalledProcessError(
            staged_check.returncode,
            staged_check.args,
            staged_check.stdout,
            staged_check.stderr,
        )

    subprocess.run(["git", "commit", "-m", message], check=True)
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
    """Return current HEAD SHA, or None if unavailable."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def git_remote_head_sha(branch: str | None) -> str | None:
    """Return the remote head SHA for a branch, or None when missing."""
    if not branch:
        return None
    result = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    first_line = (result.stdout.strip().splitlines() or [""])[0]
    if not first_line:
        return None
    sha = first_line.split()[0] if first_line.split() else ""
    return sha or None


def git_is_ancestor(older_sha: str, newer_sha: str) -> bool:
    """Return True when older_sha is an ancestor of newer_sha."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older_sha, newer_sha],
        capture_output=True,
        text=True,
        check=False,
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


def git_rebase_in_progress() -> bool:
    """Return True when repository is in an active rebase state."""
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "REBASE_HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return True

    git_dir_result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if git_dir_result.returncode != 0:
        return False
    git_dir = Path(git_dir_result.stdout.strip())
    return (git_dir / "rebase-apply").exists() or (git_dir / "rebase-merge").exists()


def git_push_force_with_lease(branch: str, expected_remote_sha: str | None) -> GitPushResult:
    """Push rewritten history safely with force-with-lease protection."""
    command = [
        "git",
        "push",
        "origin",
        f"HEAD:refs/heads/{branch}",
        "--force-with-lease",
    ]
    if expected_remote_sha:
        command.append(f"--force-with-lease=refs/heads/{branch}:{expected_remote_sha}")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return GitPushResult(
        command=tuple(command),
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


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
    subprocess.run(["git", "push"], check=True)


def git_push_head_to_branch(branch: str, debug: Callable[[int, str], None]) -> None:
    """Push HEAD to branch, retrying once with fetch/rebase if needed."""
    push_cmd = ["git", "push", "origin", f"HEAD:refs/heads/{branch}"]
    push_result = subprocess.run(push_cmd, capture_output=True, text=True)
    if push_result.returncode == 0:
        return

    debug(1, f"Push rejected for {branch}; attempting fetch/rebase.")

    fetch_result = subprocess.run(
        ["git", "fetch", "origin", branch], capture_output=True, text=True
    )
    if fetch_result.returncode != 0:
        raise subprocess.CalledProcessError(
            fetch_result.returncode,
            fetch_result.args,
            fetch_result.stdout,
            fetch_result.stderr or push_result.stderr,
        )

    rebase_target = f"origin/{branch}"
    rebase_result = subprocess.run(["git", "rebase", rebase_target], capture_output=True, text=True)
    if rebase_result.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], check=False)
        raise subprocess.CalledProcessError(
            rebase_result.returncode,
            rebase_result.args,
            rebase_result.stdout,
            rebase_result.stderr,
        )

    final_push_result = subprocess.run(push_cmd, capture_output=True, text=True)
    if final_push_result.returncode != 0:
        raise subprocess.CalledProcessError(
            final_push_result.returncode,
            final_push_result.args,
            final_push_result.stdout,
            final_push_result.stderr,
        )


def git_head_is_ahead(branch: str | None) -> bool:
    """Return True if HEAD has commits that are not on the remote branch."""
    ref = None
    if branch:
        ref = f"origin/{branch}"
    else:
        upstream_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True,
            text=True,
        )
        if upstream_result.returncode == 0:
            ref = upstream_result.stdout.strip()

    if not ref:
        return True

    ls_remote_result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch or ""],
        capture_output=True,
    )
    if ls_remote_result.returncode != 0:
        return True

    compare_result = subprocess.run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...{ref}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if compare_result.returncode != 0:
        return False

    parts = (compare_result.stdout.strip() or "0\t0").split()
    try:
        ahead = int(parts[0]) if parts else 0
    except ValueError:
        ahead = 0
    return ahead > 0


def _collect_changed_paths() -> set[str]:
    paths: set[str] = set()
    commands = [
        ["git", "diff", "--name-only", "--"],
        ["git", "diff", "--cached", "--name-only", "--"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]

    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode not in (0, 1):
            continue
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
