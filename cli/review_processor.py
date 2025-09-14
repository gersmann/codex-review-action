from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from github import Github
from github.GithubException import GithubException
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest
from github.PullRequestComment import PullRequestComment

from codex.config import (
    ApprovalPolicy,
    CodexConfig,
    ReasoningEffort,
    SandboxMode,
    SandboxWorkspaceWrite,
)
from codex.native import start_exec_stream as native_start_exec_stream

from .anchor_engine import build_maps as build_anchor_maps
from .anchor_engine import resolve_range
from .config import ReviewConfig
from .exceptions import CodexExecutionError
from .patch_parser import annotate_patch_with_line_numbers
from .prompt_builder import PromptBuilder


class ReviewProcessor:
    """Main processor for code review operations (PyGithub-based)."""

    def __init__(self, config: ReviewConfig) -> None:
        self.config = config
        self.prompt_builder = PromptBuilder(config)
        self._gh: Github | None = None

    def _debug(self, level: int, message: str) -> None:
        if self.config.debug_level >= level:
            print(f"[debug{level}] {message}", file=sys.stderr)

    def _run_codex(
        self,
        prompt: str,
        *,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        suppress_stream: bool = False,
    ) -> str:
        """Execute Codex with the given prompt and return the response.

        model_name/reasoning_effort override the defaults for fast dedup passes.
        When suppress_stream is True, do not print streamed tokens to stdout.
        """
        model = (model_name or self.config.model_name) or self.config.model_name
        effort_str = (reasoning_effort or self.config.reasoning_effort or "").lower() or "medium"
        try:
            effort_enum: ReasoningEffort | None = ReasoningEffort(effort_str)
        except ValueError:
            effort_enum = None

        overrides = CodexConfig(
            approval_policy=ApprovalPolicy.NEVER,
            include_plan_tool=False,
            include_apply_patch_tool=False,
            include_view_image_tool=False,
            show_raw_agent_reasoning=False,
            model=model,
            model_reasoning_effort=effort_enum,
            model_provider=self.config.model_provider,
        ).to_dict()

        last_msg: str | None = None
        buf_parts: list[str] = []

        try:
            stream = native_start_exec_stream(
                prompt,
                config_overrides=overrides,
                load_default_config=False,
            )

            for item in stream:
                msg = item.get("msg") if isinstance(item, dict) else None
                msg_type = msg.get("type") if isinstance(msg, dict) else None

                if self.config.debug_level >= 1:
                    if msg_type in ("agent_reasoning_delta", "agent_message_delta") and isinstance(
                        msg, dict
                    ):
                        d = msg.get("delta")
                        if isinstance(d, str):
                            # Emit only the raw text, no wrappers or newlines
                            d_one_line = d.replace("\n", "").replace("\r", "")
                            print(d_one_line, end="", file=sys.stderr)
                    else:
                        detail = None
                        if isinstance(msg, dict) and msg_type in (
                            "error",
                            "stream_error",
                            "background_event",
                        ):
                            detail = msg.get("message")
                        if detail:
                            self._debug(1, f"[codex-event] {msg_type}: {detail}")
                        else:
                            self._debug(1, f"[codex-event] {msg_type}: {msg}")

                if msg_type == "agent_message":
                    text = msg.get("message") if isinstance(msg, dict) else None
                    if isinstance(text, str):
                        last_msg = text
                        buf_parts.append(text)
                        if self.config.stream_output and not suppress_stream:
                            if buf_parts:
                                print("", file=sys.stdout)
                            print(text, end="", flush=True)

                elif msg_type == "agent_message_delta":
                    delta = msg.get("delta") if isinstance(msg, dict) else None
                    if isinstance(delta, str):
                        buf_parts.append(delta)
                        if self.config.stream_output and not suppress_stream:
                            print(delta, end="", flush=True)

                elif msg_type == "task_complete":
                    last_agent_message = (
                        msg.get("last_agent_message") if isinstance(msg, dict) else None
                    )
                    if isinstance(last_agent_message, str) and not last_msg:
                        last_msg = last_agent_message
                    if self.config.stream_output and not suppress_stream:
                        print("", file=sys.stdout, flush=True)

        except Exception as e:
            raise CodexExecutionError(f"Codex execution failed: {e}") from e

        if not last_msg:
            combined = "".join(buf_parts).strip()
            if combined:
                return combined
            raise CodexExecutionError("Codex did not return an agent message.")

        return last_msg

    def process_review(self, pr_number: int | None = None) -> dict[str, Any]:
        """Process a code review for the given pull request."""
        if pr_number is None:
            pr_number = self.config.pr_number
        if pr_number is None:
            raise ValueError("PR number must be provided")

        self._debug(1, f"Processing review for {self.config.repository} PR #{pr_number}")

        # Initialize PyGithub client and fetch PR
        gh = self._gh or Github(login_or_token=self.config.github_token, per_page=100)
        self._gh = gh
        repo = gh.get_repo(f"{self.config.owner}/{self.config.repo_name}")
        pr = repo.get_pull(pr_number)

        # Extract PR data (typed access)
        if not isinstance(pr, PullRequest):
            raise ValueError("Expected a PullRequest instance")

        head = pr.head
        base = pr.base
        pr_data = {
            "number": pr.number,
            "state": pr.state,
            "title": pr.title or "",
            "head": {"sha": head.sha if head else None, "label": head.label if head else ""},
            "base": {"sha": base.sha if base else None, "label": base.label if base else ""},
        }

        changed_files = list(pr.get_files())
        # Map old->new paths for renamed files so we can anchor against HEAD paths
        rename_map: dict[str, str] = {}
        for f in changed_files:
            try:
                if getattr(f, "status", "") == "renamed":
                    prev = getattr(f, "previous_filename", None)
                    if prev:
                        rename_map[prev] = getattr(f, "filename", prev)
            except Exception:
                pass

        head_sha = (head.sha if head else None) or pr_data.get("head", {}).get("sha")
        if not head_sha:
            raise ValueError("Missing head commit SHA")

        self._debug(1, f"Changed files: {len(changed_files)}")
        for file in changed_files[:10]:  # Log first 10 files
            patch_len = len((file.patch or "").splitlines()) if getattr(file, "patch", None) else 0
            self._debug(
                2,
                f" - {file.filename} status={getattr(file, 'status', 'modified')} patch_len={patch_len}",
            )

        # Prepare local context artifacts (diffs + PR contents with comments)
        try:
            self._write_context_artifacts(pr, changed_files)
        except Exception as e:
            # Non-fatal: continue review even if context writing fails
            self._debug(1, f"Failed to write context artifacts: {e}")

        # Load guidelines and compose prompt
        guidelines = self.prompt_builder.load_guidelines()
        prompt = self.prompt_builder.compose_prompt(guidelines, changed_files, pr_data)

        self._debug(2, f"Prompt length: {len(prompt)} chars")
        print("Running Codex to generate review findings...", flush=True)

        # Execute Codex
        output = self._run_codex(prompt)

        # Parse JSON response (robust to fenced or prefixed/suffixed text)
        try:
            result = self._parse_json_response(output)
        except json.JSONDecodeError as e:
            print("Model did not return valid JSON:")
            print(output)
            raise CodexExecutionError(f"JSON parsing error: {e}") from e

        # Process and post results
        self._post_results(result, changed_files, repo, pr, head_sha, rename_map)

        return result

    def _parse_json_response(self, text: str) -> dict[str, Any]:
        """Parse a JSON object from model output that may include code fences or extra text.

        Strategy:
        1) If fenced with ``` or ```json, strip the fences and parse.
        2) Otherwise, find the first '{' and the last '}' and attempt to parse that slice.
        3) Raise JSONDecodeError if still invalid.
        """
        s = text.strip()
        fence_match = re.match(r"^```(?:json)?\n([\s\S]*?)\n```\s*$", s)
        if fence_match:
            inner = fence_match.group(1)
            return json.loads(inner)

        # Fallback: extract the outermost JSON object by slicing
        first = s.find("{")
        last = s.rfind("}")
        if first != -1 and last != -1 and last > first:
            candidate = s[first : last + 1]
            return json.loads(candidate)

        # Final attempt: remove any lone fences that didn't match above
        s2 = re.sub(r"^```.*?$|```$", "", s, flags=re.MULTILINE).strip()
        return json.loads(s2)

    def process_edit_command(
        self, command_text: str, pr_number: int, comment_ctx: dict | None = None
    ) -> int:
        """Run a coding-agent edit command against the PR's branch.

        - Enables apply_patch tool and plan tool
        - Sets approval policy to AUTO so the agent can apply patches without prompts
        - After the run, commits and pushes changes (unless dry_run)
        """
        self._debug(1, f"Edit command on PR #{pr_number}: {command_text[:120]}")

        # Initialize PyGithub and fetch PR for branch info
        gh = self._gh or Github(login_or_token=self.config.github_token, per_page=100)
        self._gh = gh
        repo = gh.get_repo(f"{self.config.owner}/{self.config.repo_name}")
        pr = repo.get_pull(pr_number)
        head_branch = pr.head.ref if pr.head else None

        repo_root = self.config.repo_root or Path(".").resolve()
        base_instructions = (
            "You are a coding agent with write access to this repository.\n"
            f"Repository root: {repo_root}\n"
            "Follow the user's command below. Make focused changes with minimal diff.\n"
            "Use the apply_patch tool to edit files. Create directories/files as needed.\n"
            "Add or update small docs as necessary.\n"
            "Do not change unrelated code.\n"
        )

        # Add custom act instructions if provided
        instructions = base_instructions
        if self.config.act_instructions:
            instructions += f"\n{self.config.act_instructions}\n"
        prompt = (
            f"{instructions}\n\nUser command:\n{command_text}\n\n"
            "When finished, ensure the repo builds/tests if applicable."
        )

        # Run Codex agent with tools enabled and a writable workspace sandbox (act mode)
        # Enable file writes within the repo root and network access for dependency ops.
        # Fall back gracefully if sandbox types are not available in this codex-python version.
        approval = ApprovalPolicy.NEVER
        sandbox_mode = SandboxMode.WORKSPACE_WRITE
        sandbox_ws = (
            SandboxWorkspaceWrite(
                writable_roots=[str(repo_root)],
                network_access=True,
                exclude_tmpdir_env_var=False,
                exclude_slash_tmp=False,
            )
            if SandboxWorkspaceWrite and sandbox_mode is not None
            else None
        )

        overrides = CodexConfig(
            approval_policy=approval,
            include_plan_tool=True,
            include_apply_patch_tool=True,
            include_view_image_tool=False,
            show_raw_agent_reasoning=False,
            model=self.config.model_name,
            model_reasoning_effort=ReasoningEffort(self.config.reasoning_effort.lower()),
            model_provider=self.config.model_provider,
            sandbox_mode=sandbox_mode,
            sandbox_workspace_write=sandbox_ws,
        ).to_dict()

        agent_last: str | None = None
        try:
            stream = native_start_exec_stream(
                prompt,
                config_overrides=overrides,
                load_default_config=False,
            )
            # Stream to console if enabled
            for item in stream:
                msg = item.get("msg") if isinstance(item, dict) else None
                msg_type = msg.get("type") if isinstance(msg, dict) else None
                if self.config.debug_level >= 1:
                    if msg_type in ("agent_reasoning_delta", "agent_message_delta") and isinstance(
                        msg, dict
                    ):
                        d = msg.get("delta")
                        if isinstance(d, str):
                            d_one_line = d.replace("\n", "").replace("\r", "")
                            print(d_one_line, end="", file=sys.stderr)

                if self.config.stream_output and msg_type in (
                    "agent_message",
                    "agent_message_delta",
                ):
                    if isinstance(msg, dict):
                        text_val = (
                            msg.get("message") if msg_type == "agent_message" else msg.get("delta")
                        )
                        if isinstance(text_val, str):
                            print(text_val, end="", flush=True)
                if msg_type == "agent_message":
                    if isinstance(msg, dict):
                        _t = msg.get("message")
                        if isinstance(_t, str):
                            agent_last = _t
                if msg_type == "task_complete" and self.config.stream_output:
                    print("", flush=True)
        except Exception as e:
            print(f"Edit execution failed: {e}", file=sys.stderr)
            try:
                if comment_ctx:
                    self._reply_to_comment(repo, pr, comment_ctx, f"Edit failed: {e}")
            except Exception:
                pass
            return 1

        # Commit and push if there are changes
        try:
            changed = self._git_has_changes()
            if not changed:
                print("No changes to commit.")
                try:
                    if comment_ctx:
                        self._reply_to_comment(
                            repo,
                            pr,
                            comment_ctx,
                            self._format_edit_reply(
                                agent_last or "(no output)",
                                pushed=False,
                                dry_run=self.config.dry_run,
                                changed=False,
                            ),
                        )
                except Exception:
                    pass
                return 0
            if self.config.dry_run:
                print("DRY_RUN: would commit and push changes.")
                self._git_status_pretty()
                try:
                    if comment_ctx:
                        self._reply_to_comment(
                            repo,
                            pr,
                            comment_ctx,
                            self._format_edit_reply(
                                agent_last or "(no output)",
                                pushed=False,
                                dry_run=True,
                                changed=True,
                            ),
                        )
                except Exception:
                    pass
                return 0
            self._git_setup_identity()
            self._git_commit_all(f"Codex edit: {command_text.splitlines()[0][:72]}")
            if head_branch:
                self._git_push_head_to_branch(head_branch)
            else:
                # Fallback: push current branch
                subprocess.run(["git", "push"], check=True)
            print("Pushed edits successfully.")
            try:
                if comment_ctx:
                    self._reply_to_comment(
                        repo,
                        pr,
                        comment_ctx,
                        self._format_edit_reply(
                            agent_last or "(no output)", pushed=True, dry_run=False, changed=True
                        ),
                    )
            except Exception:
                pass
            return 0
        except subprocess.CalledProcessError as e:
            print(f"Git operation failed: {e}", file=sys.stderr)
            try:
                if comment_ctx:
                    self._reply_to_comment(repo, pr, comment_ctx, f"Git operation failed: {e}")
            except Exception:
                pass
            return 2

    def _format_edit_reply(
        self, agent_output: str, *, pushed: bool, dry_run: bool, changed: bool
    ) -> str:
        status = (
            "dry-run (no push)"
            if dry_run
            else ("pushed changes" if pushed else ("no changes" if not changed else "not pushed"))
        )
        header = f"Codex edit result ({status}):"
        body = (agent_output or "").strip()
        if len(body) > 3500:
            body = body[:3500] + "\n\n… (truncated)"
        return f"{header}\n\n{body}"

    def _reply_to_comment(self, repo, pr, comment_ctx: dict, text: str) -> None:
        event = (comment_ctx.get("event_name") or "").lower()
        comment_id = int(comment_ctx.get("id") or 0)
        if not comment_id:
            pr.as_issue().create_comment(text)
            return
        try:
            if event == "pull_request_review_comment":
                url = f"{pr.url}/comments/{comment_id}/replies"
                pr._requester.requestJsonAndCheck("POST", url, input={"body": text})
            elif event == "issue_comment":
                pr.as_issue().create_comment(text)
            else:
                pr.as_issue().create_comment(text)
        except Exception as e:
            self._debug(1, f"Failed replying to comment {comment_id}: {e}")

    def _git_has_changes(self) -> bool:
        res = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        return bool(res.stdout.strip())

    def _git_status_pretty(self) -> None:
        subprocess.run(["git", "status", "--short"], check=False)

    def _git_setup_identity(self) -> None:
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)

    def _git_commit_all(self, message: str) -> None:
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)

    def _git_push_head_to_branch(self, branch: str) -> None:
        # Push current HEAD to the target branch name on origin (works in detached HEAD)
        subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], check=True)

    def _write_context_artifacts(self, pr, changed_files: list) -> None:
        """Create a `.codex-context` directory with diffs and PR context (including comments)."""
        repo_root = self.config.repo_root or Path(".").resolve()
        base_dir_name = self.config.context_dir_name or ".codex-context"
        base_dir = (repo_root / base_dir_name).resolve()

        diffs_dir = base_dir / "diffs"
        annotated_dir = base_dir / "diffs_annotated"
        diffs_dir.mkdir(parents=True, exist_ok=True)
        annotated_dir.mkdir(parents=True, exist_ok=True)

        # Write combined diffs file and per-file patches
        combined_lines: list[str] = []
        for file in changed_files:
            filename = file.filename
            patch = getattr(file, "patch", None)
            status = getattr(file, "status", "modified")
            if not filename or not patch:
                continue

            combined_lines.append(f"File: {filename}\nStatus: {status}\n---\n{patch}\n")

            # Create subdirs mirroring the file path and write .patch
            file_path = Path(filename)
            target_dir = diffs_dir / file_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{file_path.name}.patch").write_text(patch, encoding="utf-8")

            # Also write annotated diff with explicit BASE/HEAD numbers
            a_target_dir = annotated_dir / file_path.parent
            a_target_dir.mkdir(parents=True, exist_ok=True)
            annotated = annotate_patch_with_line_numbers(patch)
            (a_target_dir / f"{file_path.name}.annotated.patch").write_text(
                annotated, encoding="utf-8"
            )

        (base_dir / "combined_diffs.txt").write_text(
            "\n" + ("\n" + ("-" * 80) + "\n").join(combined_lines), encoding="utf-8"
        )

        # Write PR metadata and comments into pr.md
        parts: list[str] = []
        parts.append(f"PR #{pr.number}: {pr.title or ''}")
        parts.append("")
        parts.append(f"URL: {pr.html_url}")
        parts.append(f"Author: {pr.user.login if pr.user else ''}")
        parts.append(f"State: {pr.state}")
        parts.append("")
        body = pr.body or ""
        if body:
            parts.append("PR Description:\n")
            parts.append(body)
            parts.append("")

        # Issue comments (a.k.a. conversation comments)
        try:
            issue_comments = list(pr.get_issue_comments())
        except Exception:
            issue_comments = []
        if issue_comments:
            parts.append("Issue Comments:")
            for c in issue_comments:
                if isinstance(c, IssueComment):
                    author = c.user.login if c.user else ""
                    created = c.created_at
                    parts.append(f"- [{created}] @{author}:\n{c.body or ''}\n")

        # Review comments (inline on diffs)
        try:
            review_comments = list(pr.get_review_comments())
        except Exception:
            review_comments = []
        if review_comments:
            parts.append("Review Comments:")
            for rc in review_comments:
                if isinstance(rc, PullRequestComment):
                    author = rc.user.login if rc.user else ""
                    created = rc.created_at
                    path = rc.path or ""
                    line = rc.line or rc.original_line
                    parts.append(f"- [{created}] @{author} on {path}:{line}\n{rc.body or ''}\n")

        (base_dir / "pr.md").write_text("\n".join(parts) + "\n", encoding="utf-8")

    def _post_results(
        self,
        result: dict[str, Any],
        changed_files: list,
        repo,
        pr,
        head_sha: str,
        rename_map: dict[str, str],
    ) -> None:
        """Post review results to GitHub."""
        findings: list[dict[str, Any]] = list(result.get("findings", []) or [])
        total_findings = len(findings)
        overall = str(result.get("overall_correctness", "")).strip() or "patch is correct"
        overall_explanation = str(result.get("overall_explanation", "")).strip()
        overall_conf = result.get("overall_confidence_score")

        # If this PR already has a Codex review, deduplicate new findings using the fast model
        try:
            if self._has_prior_codex_review(pr):
                existing = self._collect_existing_comment_texts(pr)
                filtered = self._deduplicate_findings(findings, existing)
                if isinstance(filtered, list):
                    print(f"Dedup kept {len(filtered)}/{len(findings)} findings (fast model)")
                    findings = filtered
        except Exception as e:
            self._debug(1, f"Deduplication step failed: {e}")

        # Compose summary using total (pre-dedup) count
        summary_lines = [
            "Codex Autonomous Review:",
            f"- Overall: {overall}",
            f"- Findings (total): {total_findings}",
        ]
        if overall_explanation:
            summary_lines.append("")
            summary_lines.append(overall_explanation)
        if isinstance(overall_conf, (int, float)):
            summary_lines.append(f"Confidence: {overall_conf}")
        summary = "\n".join(summary_lines)

        # Replace previous summary with a fresh issue comment
        try:
            if self.config.dry_run:
                self._debug(
                    1, "DRY_RUN: would delete prior summary (if any) and create a fresh one"
                )
            else:
                self._delete_prior_summary(pr)
                pr.as_issue().create_comment(summary)
        except GithubException as e:
            print(f"Failed to update summary comment: {e}", file=sys.stderr)

        # Build anchor maps for inline comments (deterministic)
        file_maps = build_anchor_maps(changed_files)

        # Persist anchor maps for debugging and line mapping inspection
        try:
            repo_root = self.config.repo_root or Path(".").resolve()
            base_dir = (repo_root / (self.config.context_dir_name or ".codex-context")).resolve()
            out = {
                k: {
                    "valid_head_lines": sorted(list(v.valid_head_lines)),
                    "added_head_lines": sorted(list(v.added_head_lines)),
                    "positions_by_head_line": {
                        str(kk): vv for kk, vv in v.positions_by_head_line.items()
                    },
                    "hunks": v.hunks,
                }
                for k, v in file_maps.items()
            }
            (base_dir / "anchor_maps.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        except Exception as e:
            self._debug(1, f"Failed writing anchor_maps.json: {e}")

        # Post findings using single-comment API only (no batch, no file-level fallback)
        self._post_findings(findings, file_maps, repo, pr, head_sha, rename_map)

    def _delete_prior_summary(self, pr) -> None:
        """Delete prior Codex summary comments and dismiss prior summary reviews."""
        marker = "Codex Autonomous Review:"
        # Issue comments
        try:
            for c in pr.get_issue_comments():
                body = (getattr(c, "body", "") or "").strip()
                if marker in body:
                    try:
                        c.delete()
                        self._debug(
                            1, f"Deleted prior summary issue comment id={getattr(c, 'id', None)}"
                        )
                    except Exception as e:
                        self._debug(
                            1, f"Failed to delete issue comment id={getattr(c, 'id', None)}: {e}"
                        )
        except Exception as e:
            self._debug(1, f"Listing issue comments failed: {e}")

        # PR reviews (dismiss)
        try:
            requester = getattr(pr, "_requester", None)
            pr_url = getattr(pr, "url", "")
            for r in pr.get_reviews():
                body = (getattr(r, "body", "") or "").strip()
                if marker in body:
                    review_id = getattr(r, "id", None)
                    if requester and pr_url and review_id:
                        url = f"{pr_url}/reviews/{review_id}/dismissals"
                        payload = {"message": "Superseded by latest Codex review."}
                        try:
                            requester.requestJsonAndCheck("PUT", url, input=payload)
                            self._debug(1, f"Dismissed prior PR review id={review_id}")
                        except Exception as e:
                            self._debug(1, f"Failed to dismiss review id={review_id}: {e}")
        except Exception as e:
            self._debug(1, f"Listing/dismissing reviews failed: {e}")

    def _has_prior_codex_review(self, pr) -> bool:
        try:
            for rev in pr.get_reviews():
                body = rev.body or ""
                if "Codex Autonomous Review:" in body:
                    return True
        except Exception:
            pass
        # Also check issue comments, in case summary was posted there in previous versions
        try:
            for c in pr.get_issue_comments():
                if isinstance(c, IssueComment) and "Codex Autonomous Review:" in (c.body or ""):
                    return True
        except Exception:
            pass
        return False

    def _collect_existing_comment_texts(self, pr) -> list[str]:
        """Collect only file/diff review comments for deduplication.

        Excludes PR-level summaries and issue comments so they don't suppress
        per-file findings.
        """
        texts: list[str] = []
        try:
            for rc in pr.get_review_comments():
                if isinstance(rc, PullRequestComment):
                    body = (rc.body or "").strip()
                    path = getattr(rc, "path", "") or ""
                    line = getattr(rc, "line", None) or getattr(rc, "original_line", None)
                    if body:
                        loc = f"{path}:{line}" if path and line else path or ""
                        prefix = f"[{loc}] " if loc else ""
                        texts.append(prefix + body)
        except Exception:
            pass
        return texts

    def _deduplicate_findings(
        self, findings: list[dict[str, Any]], existing_comments: list[str]
    ) -> list[dict[str, Any]]:
        """Use the fast model to filter out findings already covered by existing comments."""
        # Build compact payload
        compact_findings: list[dict[str, Any]] = []
        for idx, f in enumerate(findings):
            loc = (f.get("code_location") or {}) if isinstance(f, dict) else {}
            rng = (loc.get("line_range") or {}) if isinstance(loc, dict) else {}
            compact_findings.append(
                {
                    "index": idx,
                    "title": str(f.get("title", "")),
                    "body": str(f.get("body", "")),
                    "path": str(loc.get("absolute_file_path", "")),
                    "start": int(rng.get("start", 0) or 0),
                }
            )

        instructions = (
            "You are deduplicating review comments.\n"
            'Given `new_findings` and `existing_comments`, return JSON {"keep": [indices]} where indices refer to new_findings[index].\n'
            "Consider a new finding a duplicate if an existing comment already conveys the same issue for the same file and nearby lines,\n"
            "or if it is semantically redundant. Prefer recall (keep) when unsure.\n"
        )

        payload = {
            "new_findings": compact_findings,
            "existing_comments": existing_comments[:200],  # cap to avoid huge prompts
        }

        prompt = (
            instructions
            + "\n\nINPUT:\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\n\nOUTPUT: JSON with only the 'keep' array."
        )

        raw = self._run_codex(
            prompt,
            model_name=self.config.fast_model_name,
            reasoning_effort=self.config.fast_reasoning_effort,
            suppress_stream=True,
        )
        try:
            data = json.loads(raw)
            keep = data.get("keep") if isinstance(data, dict) else None
            if not isinstance(keep, list):
                return findings
            keep_set = {
                int(i)
                for i in keep
                if isinstance(i, int) or (isinstance(i, str) and str(i).isdigit())
            }
            return [f for i, f in enumerate(findings) if i in keep_set]
        except Exception:
            return findings

    def _post_findings(
        self,
        findings: list[dict[str, Any]],
        file_maps: dict,
        repo,
        pr,
        head_sha: str,
        rename_map: dict[str, str],
    ) -> None:
        """Post findings using deterministic anchors via single-comment API only."""
        repo_root = self.config.repo_root or Path(".").resolve()

        for finding in findings:
            title = str(finding.get("title", "Issue")).strip()
            body = str(finding.get("body", "")).strip()
            location = finding.get("code_location", {}) or {}
            abs_path = str(location.get("absolute_file_path", "")).strip()
            rng = location.get("line_range", {}) or {}
            start_line = int(rng.get("start", 0) or 0)
            end_line = int(rng.get("end", start_line) or start_line)

            if not abs_path or start_line <= 0:
                continue

            # Convert absolute path to repo-relative
            try:
                rel_path = str(Path(abs_path).resolve().relative_to(repo_root))
            except ValueError:
                rel_path = abs_path.lstrip("./")
            rel_path = rename_map.get(rel_path, rel_path)

            fmap = file_maps.get(rel_path)
            if not fmap:
                continue

            has_suggestion = "```suggestion" in body
            anchor = resolve_range(rel_path, start_line, end_line, has_suggestion, fmap)
            if not anchor:
                if self.config.dry_run:
                    self._debug(
                        1, f"DRY_RUN: would skip (no anchor) for {rel_path}:{start_line}-{end_line}"
                    )
                continue

            final_body = body
            if has_suggestion and not (
                anchor.get("allow_suggestion") and anchor.get("kind") == "range"
            ):
                final_body = body.replace("```suggestion", "```diff")

            comment_body = f"{title}\n\n{final_body}" if final_body else title

            if self.config.dry_run:
                if anchor["kind"] == "range":
                    self._debug(
                        1,
                        f"DRY_RUN: would post RANGE {rel_path}:{anchor['start_line']}-{anchor['end_line']}",
                    )
                else:
                    self._debug(1, f"DRY_RUN: would post SINGLE {rel_path}:{anchor['line']}")
                continue

            url = f"{pr.url}/comments"
            payload: dict[str, Any] = {
                "body": comment_body,
                "commit_id": head_sha,
                "path": rel_path,
                "side": "RIGHT",
            }
            if anchor["kind"] == "range":
                payload["start_line"] = int(anchor["start_line"])
                payload["line"] = int(anchor["end_line"])
            else:
                payload["line"] = int(anchor["line"])

            try:
                pr._requester.requestJsonAndCheck("POST", url, input=payload)
                time.sleep(0.05)
            except Exception as e:
                self._debug(1, f"Failed to post comment for {rel_path}: {e}")
