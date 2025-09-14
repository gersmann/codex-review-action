#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from codex.config import ApprovalPolicy, CodexConfig, ReasoningEffort
from codex.native import start_exec_stream as native_start_exec_stream


@dataclass
class ChangedFile:
    filename: str
    sha: str
    status: str
    patch: str | None


def _debug_level() -> int:
    try:
        return int(os.environ.get("DEBUG_CODEREVIEW", "0").strip() or 0)
    except Exception:
        return 0


def _dbg(level: int, message: str) -> None:
    if _debug_level() >= level:
        print(f"[debug{level}] {message}", file=sys.stderr)




def _github_api(
    method: str,
    url: str,
    *,
    token: str,
    data: dict | None = None,
    params: dict | None = None,
    accept: str = "application/vnd.github+json",
) -> Any:
    if params:
        sep = "&" if ("?" in url) else "?"
        url = f"{url}{sep}{urlencode(params)}"
    body: bytes | None = json.dumps(data).encode("utf-8") if data is not None else None
    req = Request(url=url, method=method, data=body)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        req.add_header("Content-Type", "application/json")

    if _debug_level() >= 2:
        preview = None
        if data is not None:
            try:
                preview = json.dumps(data)[:800]
            except Exception:
                preview = str(data)[:800]
        _dbg(2, f"HTTP {method} {url} accept={accept} data={preview}")

    if os.environ.get("DRY_RUN") == "1" and method.upper() == "POST":
        _dbg(1, f"DRY_RUN: skipping POST {url}")
        return {"dry_run": True, "url": url, "data": data}

    try:
        with urlopen(req) as resp:  # nosec - GitHub API
            ctype = resp.headers.get("Content-Type", "")
            code = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
            if _debug_level() >= 2:
                _dbg(2, f"HTTP {method} {url} -> {code} ct={ctype} len={len(raw)}")
            if "application/json" in ctype:
                return json.loads(raw.decode("utf-8"))
            return raw
    except HTTPError as e:
        try:
            raw = e.read()
            msg = raw.decode("utf-8")
            print(f"GitHub API error body: {msg}", file=sys.stderr)
        except Exception:
            pass
        raise


def _parse_valid_head_lines_from_patch(patch: str) -> set[int]:
    valid: set[int] = set()
    i_old = i_new = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            # Header looks like: @@ -a,b +c,d @@ optional
            try:
                header = line.split("@@")[1]
            except Exception:
                header = line
            tokens = header.strip().split()
            # Find a token starting with '+'
            plus = next((t for t in tokens if t.startswith("+")), "+0,0")
            minus = next((t for t in tokens if t.startswith("-")), "-0,0")
            try:
                i_new = int(plus[1:].split(",")[0]) - 1
                i_old = int(minus[1:].split(",")[0]) - 1
            except Exception:
                i_new = i_old = 0
            continue
        if not line:
            continue
        tag = line[0]
        if tag == " ":
            i_old += 1
            i_new += 1
            valid.add(i_new)
        elif tag == "+":
            i_new += 1
            valid.add(i_new)
        elif tag == "-":
            i_old += 1
        else:
            pass
    return valid


def _compute_position_from_patch(patch: str, target_head_line: int) -> int | None:
    pos = 0
    i_old = i_new = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            try:
                header = line.split("@@")[1]
            except Exception:
                header = line
            tokens = header.strip().split()
            plus = next((t for t in tokens if t.startswith("+")), "+0,0")
            minus = next((t for t in tokens if t.startswith("-")), "-0,0")
            try:
                i_new = int(plus[1:].split(",")[0]) - 1
                i_old = int(minus[1:].split(",")[0]) - 1
            except Exception:
                i_new = i_old = 0
            continue
        if not in_hunk or not line:
            continue
        tag = line[0]
        pos += 1
        if tag == " ":
            i_old += 1
            i_new += 1
            if i_new == target_head_line:
                return pos
        elif tag == "+":
            i_new += 1
            if i_new == target_head_line:
                return pos
        elif tag == "-":
            i_old += 1
        else:
            pass
    return None


def _get_event() -> dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print("GITHUB_EVENT_PATH not set; are we in GitHub Actions?", file=sys.stderr)
        sys.exit(1)
    with open(event_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        print("Unexpected event payload type; expected object", file=sys.stderr)
        sys.exit(1)
    return cast(dict[str, Any], data)


def _load_guidelines() -> str:
    strategy = (os.environ.get("REVIEW_PROMPT_STRATEGY") or "auto").strip().lower()
    inline = os.environ.get("REVIEW_PROMPT_INLINE") or ""
    path_str = os.environ.get("REVIEW_PROMPT_PATH") or "prompts/code-review.md"
    path = Path(path_str)
    # Built-in file packaged with the action
    builtin_path = Path(__file__).resolve().parents[1] / "prompts" / "review.md"

    def read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8")
        except Exception as e:
            _dbg(1, f"Failed reading prompt file {p}: {e}")
            raise

    def use_file() -> bool:
        return path.exists() and path.is_file()

    if strategy == "inline" and inline:
        _dbg(1, "Using inline prompt (env)")
        return inline
    if strategy == "file" and use_file():
        _dbg(1, f"Using file prompt: {path}")
        return read(path)
    if strategy == "builtin":
        _dbg(1, f"Using builtin prompt: {builtin_path}")
        return read(builtin_path)

    # auto: prefer inline > file > builtin
    if inline:
        _dbg(1, "Using inline prompt (auto)")
        return inline
    if use_file():
        _dbg(1, f"Using file prompt (auto): {path}")
        return read(path)
    _dbg(1, f"Using builtin prompt (auto fallback): {builtin_path}")
    return read(builtin_path)


def _list_changed_files(owner: str, repo: str, pr_number: int, token: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    page = 1
    per_page = 100
    while True:
        batch = _github_api(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files",
            token=token,
            params={"page": page, "per_page": per_page},
        )
        if not batch:
            break
        for it in batch:
            files.append(
                ChangedFile(
                    filename=it["filename"],
                    sha=it["sha"],
                    status=it.get("status", "modified"),
                    patch=it.get("patch"),
                )
            )
        if len(batch) < per_page:
            break
        page += 1
    return files


def _get_pr(owner: str, repo: str, pr_number: int, token: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _github_api(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            token=token,
        ),
    )


def _compose_prompt(guidelines: str, repo_root: Path, changed: list[ChangedFile], pr: dict) -> str:
    pr_title = pr.get("title") or ""
    head_label = pr.get("head", {}).get("label", "")
    base_label = pr.get("base", {}).get("label", "")
    head_sha = pr.get("head", {}).get("sha", "")
    base_sha = pr.get("base", {}).get("sha", "")

    intro = (
        "You are an autonomous code review assistant.\n"
        "Carefully read the guidelines and analyze ONLY the provided diffs.\n"
        "Output exactly the JSON as specified. Do not add fences or extra text.\n"
    )

    context = (
        f"PR Title: {pr_title}\n"
        f"From: {head_label} ({head_sha}) -> To: {base_label} ({base_sha})\n\n"
        "Important paths:\n"
        f"- Repo root (absolute): {repo_root}\n"
        "When returning code_location.absolute_file_path, use the absolute path under this root.\n"
        "Line ranges must overlap with the provided diff hunks.\n"
    )

    diffs: list[str] = []
    for f in changed:
        if not f.patch:
            continue
        diffs.append(
            f"File: {f.filename}\nStatus: {f.status}\nPatch (unified diff):\n---\n{f.patch}\n"
        )

    diff_blob = (
        ("\n" + ("\n" + ("-" * 80) + "\n").join(diffs)) if diffs else "\n(no diff patch content available)\n"
    )

    prompt = (
        f"{intro}\n\n"
        "Review guidelines (verbatim):\n"
        f"{guidelines}\n\n"
        f"{context}\n"
        "Changed files and patches:\n"
        f"{diff_blob}\n\n"
        "Respond now with the JSON schema output only."
    )
    return prompt


def _ensure_model_auth() -> None:
    provider = os.environ.get("CODEX_PROVIDER", "openai").strip().lower()
    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            print(
                "Missing OPENAI_API_KEY for model provider 'openai'. ",
                file=sys.stderr,
            )
            sys.exit(2)


def _run_codex(prompt: str) -> str:
    _ensure_model_auth()

    model = os.environ.get("CODEX_MODEL", "gpt-4.1-mini").strip()
    provider = os.environ.get("CODEX_PROVIDER", "openai").strip()

    reasoning_effort = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip()
    try:
        effort_enum: ReasoningEffort | None = ReasoningEffort(reasoning_effort.lower())
    except Exception:
        effort_enum = None

    overrides = CodexConfig(
        approval_policy=ApprovalPolicy.NEVER,
        include_plan_tool=False,
        include_apply_patch_tool=False,
        include_view_image_tool=False,
        show_raw_agent_reasoning=False,
        model=model,
        model_reasoning_effort=effort_enum,
        model_provider=provider,
        base_instructions=(
            "You are a precise code review assistant.\n"
            "You must respond with a single JSON object, matching the provided schema exactly.\n"
            "Do not include any Markdown fences or extra commentary.\n"
            f"Target reasoning effort: {reasoning_effort}."
        ),
    ).to_dict()

    last_msg: str | None = None
    _buf_parts: list[str] = []
    debug = _debug_level() >= 1
    stream_stdout = os.environ.get("STREAM_AGENT_MESSAGES", "1") != "0"

    stream = native_start_exec_stream(
        prompt,
        config_overrides=overrides,
        load_default_config=False,
    )

    for item in stream:
        msg = item.get("msg") if isinstance(item, dict) else None
        t = msg.get("type") if isinstance(msg, dict) else None
        if debug:
            detail = None
            if isinstance(msg, dict) and t in ("error", "stream_error", "background_event"):
                detail = msg.get("message")
            if detail:
                print(f"[codex-event] {t}: {detail}", file=sys.stderr)
            else:
                print(f"[codex-event] {t}: {msg}", file=sys.stderr)
        if t == "agent_message":
            text = msg.get("message") if isinstance(msg, dict) else None
            if isinstance(text, str):
                last_msg = text
                _buf_parts.append(text)
                if stream_stdout:
                    if _buf_parts:
                        print("", file=sys.stdout)
                    print(text, end="", flush=True)
        elif t == "agent_message_delta":
            delta = msg.get("delta") if isinstance(msg, dict) else None
            if isinstance(delta, str):
                _buf_parts.append(delta)
                if stream_stdout:
                    print(delta, end="", flush=True)
        elif t == "task_complete":
            lam = msg.get("last_agent_message") if isinstance(msg, dict) else None
            if isinstance(lam, str) and not last_msg:
                last_msg = lam
            if stream_stdout:
                print("", file=sys.stdout, flush=True)

    if not last_msg:
        _combined = "".join(_buf_parts).strip()
        if _combined:
            return _combined
        raise RuntimeError("Codex did not return an agent message.")
    return last_msg


def _post_review(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
    summary: str,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _github_api(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            token=token,
            data={"event": "COMMENT", "body": summary},
        ),
    )


def _post_inline_comment(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
    commit_id: str,
    path: str,
    position: int,
    body: str,
) -> dict[str, Any]:
    payload = {"body": body, "commit_id": commit_id, "path": path, "position": position}
    return cast(
        dict[str, Any],
        _github_api(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            token=token,
            data=payload,
            accept="application/vnd.github+json, application/vnd.github.comfort-fade-preview+json",
        ),
    )


def _post_file_comment(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
    commit_id: str,
    path: str,
    body: str,
) -> dict[str, Any]:
    payload = {"body": body, "commit_id": commit_id, "path": path, "subject_type": "file"}
    return cast(
        dict[str, Any],
        _github_api(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            token=token,
            data=payload,
            accept="application/vnd.github+json, application/vnd.github.comfort-fade-preview+json",
        ),
    )


def main() -> None:
    repo_full = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo_full or "/" not in repo_full:
        print("GITHUB_REPOSITORY missing or invalid", file=sys.stderr)
        sys.exit(1)
    owner, repo = repo_full.split("/", 1)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN not provided", file=sys.stderr)
        sys.exit(1)

    event = _get_event()
    if "pull_request" not in event:
        print("This workflow must be triggered by a pull_request event", file=sys.stderr)
        sys.exit(1)
    pr_evt = event["pull_request"]
    pr_number = int(pr_evt.get("number") or event.get("number") or 0)
    pr = _get_pr(owner, repo, pr_number, token)
    head_sha = pr.get("head", {}).get("sha")
    if not head_sha:
        print("Missing head commit SHA", file=sys.stderr)
        sys.exit(1)

    changed = _list_changed_files(owner, repo, pr_number, token)

    guidelines = _load_guidelines()
    repo_root = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    prompt = _compose_prompt(guidelines, repo_root, changed, pr)

    _dbg(1, f"Repo: {owner}/{repo} PR: #{pr_number} head={head_sha}")
    _dbg(1, f"Changed files: {len(changed)}")
    for fch in changed[:10]:
        _dbg(
            2,
            f" - {fch.filename} status={fch.status} patch_len={len(fch.patch.splitlines()) if fch.patch else 0}",
        )
    _dbg(2, f"Prompt length: {len(prompt)} chars")
    print("Running Codex to generate review findings...", flush=True)

    try:
        output = _run_codex(prompt)
    except Exception as e:
        print(f"Codex execution failed: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        result = json.loads(output)
    except json.JSONDecodeError as e:
        print("Model did not return valid JSON:")
        print(output)
        print(f"JSON error: {e}", file=sys.stderr)
        sys.exit(3)

    findings: list[dict[str, Any]] = list(result.get("findings", []) or [])
    overall = str(result.get("overall_correctness", "")).strip() or "patch is correct"
    overall_explanation = str(result.get("overall_explanation", "")).strip()
    overall_conf = result.get("overall_confidence_score")

    summary_lines = [
        "Codex Autonomous Review:",
        f"- Overall: {overall}",
        f"- Findings: {len(findings)}",
    ]
    if overall_explanation:
        summary_lines.append("")
        summary_lines.append(overall_explanation)
    if isinstance(overall_conf, (int, float)):
        summary_lines.append(f"Confidence: {overall_conf}")
    summary = "\n".join(summary_lines)

    try:
        _post_review(owner=owner, repo=repo, pr_number=pr_number, token=token, summary=summary)
    except HTTPError as e:
        print(f"Failed to post review summary: {e}", file=sys.stderr)

    # Precompute anchor maps
    valid_lines_by_path: dict[str, set[int]] = {}
    position_by_path: dict[str, dict[int, int]] = {}
    for fch in changed:
        if fch.patch:
            valid_lines_by_path[fch.filename] = _parse_valid_head_lines_from_patch(fch.patch)
            pos_map: dict[int, int] = {}
            for ln in valid_lines_by_path[fch.filename]:
                pos = _compute_position_from_patch(fch.patch, ln)
                if pos is not None:
                    pos_map[ln] = pos
            position_by_path[fch.filename] = pos_map
            _dbg(2, f"Anchor map ready for {fch.filename}: valid_lines={len(valid_lines_by_path[fch.filename])} positions={len(pos_map)}")

    for f in findings:
        title = str(f.get("title", "Issue")).strip()
        body = str(f.get("body", "")).strip()
        loc = f.get("code_location", {}) or {}
        abs_path = str(loc.get("absolute_file_path", "")).strip()
        line_range = loc.get("line_range", {}) or {}
        start_line = int(line_range.get("start", 0))
        if not abs_path or start_line <= 0:
            continue
        try:
            rel_path = str(Path(abs_path).resolve().relative_to(repo_root))
        except Exception:
            rel_path = abs_path.lstrip("./")

        pos_map = position_by_path.get(rel_path, {})
        pos = pos_map.get(start_line)
        can_anchor = pos is not None
        comment_body = f"{title}\n\n{body}"
        try:
            if can_anchor and pos is not None:
                _dbg(1, f"Posting inline comment: {rel_path}:{start_line} -> position={pos}")
                _post_inline_comment(
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    token=token,
                    commit_id=head_sha,
                    path=rel_path,
                    position=pos,
                    body=comment_body,
                )
            else:
                _dbg(1, f"Posting file-level comment: {rel_path} (line {start_line} not in diff)")
                _post_file_comment(
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    token=token,
                    commit_id=head_sha,
                    path=rel_path,
                    body=comment_body + "\n\n(Note: referenced line not in diff; posting at file level.)",
                )
            time.sleep(0.2)
        except HTTPError as e:
            print(f"Failed to post comment for {rel_path}:{start_line}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
