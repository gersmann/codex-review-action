from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
import re
from typing import Any

from github import Github
from github.GithubException import GithubException
from github.PullRequest import PullRequest
from github.PullRequestComment import PullRequestComment
from github.IssueComment import IssueComment

from codex.config import ApprovalPolicy, CodexConfig, ReasoningEffort
from codex.native import start_exec_stream as native_start_exec_stream

from .config import ReviewConfig
from .exceptions import CodexExecutionError
from .patch_parser import build_anchor_maps
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
            # base_instructions=(
            #     "You are a precise code review assistant.\n"
            #     "You must respond with a single JSON object, matching the provided schema exactly.\n"
            #     "Do not include any Markdown fences or extra commentary.\n"
            #     f"Target reasoning effort: {effort_str}."
            # ),
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
                    if msg_type == "agent_reasoning_delta" and isinstance(msg, dict):
                        d = msg.get("delta")
                        # Print exact single-line event, no extra breaks
                        if isinstance(d, str):
                            print(
                                f"[debug1] [codex-event] agent_reasoning_delta: {{'delta': {d!r}, 'type': 'agent_reasoning_delta'}}",
                                file=sys.stderr,
                            )
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

    def process_edit_command(self, command_text: str, pr_number: int, comment_ctx: dict | None = None) -> int:
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

        # Run Codex agent with tools enabled
        overrides = CodexConfig(
            approval_policy=ApprovalPolicy.NEVER,
            include_plan_tool=True,
            include_apply_patch_tool=True,
            include_view_image_tool=False,
            show_raw_agent_reasoning=False,
            model=self.config.model_name,
            model_reasoning_effort=ReasoningEffort(self.config.reasoning_effort.lower()),
            model_provider=self.config.model_provider,
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
                    if msg_type == "agent_reasoning_delta" and isinstance(msg, dict):
                        d = msg.get("delta")
                        if isinstance(d, str):
                            print(
                                f"[debug1] [codex-event] agent_reasoning_delta: {{'delta': {d!r}, 'type': 'agent_reasoning_delta'}}",
                                file=sys.stderr,
                            )

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
                            self._format_edit_reply(agent_last or "(no output)", pushed=False, dry_run=self.config.dry_run, changed=False),
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
                            self._format_edit_reply(agent_last or "(no output)", pushed=False, dry_run=True, changed=True),
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
                        self._format_edit_reply(agent_last or "(no output)", pushed=True, dry_run=False, changed=True),
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

    def _format_edit_reply(self, agent_output: str, *, pushed: bool, dry_run: bool, changed: bool) -> str:
        status = (
            "dry-run (no push)" if dry_run else ("pushed changes" if pushed else ("no changes" if not changed else "not pushed"))
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
        diffs_dir.mkdir(parents=True, exist_ok=True)

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
                self._debug(1, "DRY_RUN: would delete prior summary (if any) and create a fresh one")
            else:
                self._delete_prior_summary(pr)
                pr.as_issue().create_comment(summary)
        except GithubException as e:
            print(f"Failed to update summary comment: {e}", file=sys.stderr)

        # Build anchor maps for inline comments
        valid_lines_by_path, position_by_path = build_anchor_maps(changed_files)

        for file in changed_files:
            if getattr(file, "patch", None):
                valid_count = len(valid_lines_by_path.get(file.filename, set()))
                pos_count = len(position_by_path.get(file.filename, {}))
                self._debug(
                    2,
                    f"Anchor map ready for {file.filename}: valid_lines={valid_count} positions={pos_count}",
                )

        # Post individual findings (prefer line/side anchoring with batching; fallback to file-level)
        self._post_findings(findings, valid_lines_by_path, position_by_path, repo, pr, head_sha, rename_map)

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
                        self._debug(1, f"Deleted prior summary issue comment id={getattr(c,'id',None)}")
                    except Exception as e:
                        self._debug(1, f"Failed to delete issue comment id={getattr(c,'id',None)}: {e}")
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
        valid_lines_by_path: dict[str, set[int]],
        position_by_path: dict[str, dict[int, int]],
        repo,
        pr,
        head_sha: str,
        rename_map: dict[str, str],
    ) -> None:
        """Post findings with correct anchoring and safer suggestions.

        - Prefer per-comment "line/side" API (supports start_line for multi-line).
        - Fallback to single-line inline comments if the full range is not in the diff.
        - As last resort, post a file-level comment with context and permalink.
        - Never include a multi-line ```suggestion block unless we anchor start_line/line.
        """
        repo_root = self.config.repo_root or Path(".").resolve()

        file_level_messages: dict[str, list[str]] = {}

        def has_suggestion(text: str) -> bool:
            return "```suggestion" in text

        def sanitize_suggestion_for_single_line(text: str) -> str:
            if "```suggestion" not in text:
                return text
            return text.replace("```suggestion", "```diff")

        def nearest_valid_line(path: str, target: int) -> int | None:
            valid = sorted(valid_lines_by_path.get(path, set()))
            if not valid:
                return None
            if target in valid:
                return target
            nearest = min(valid, key=lambda x: (abs(x - target), x))
            if abs(nearest - target) <= 50:
                return nearest
            return None

        def both_in_same_hunk(path: str, a: int, b: int) -> bool:
            pmap = position_by_path.get(path, {})
            return a in pmap and b in pmap

        for finding in findings:
            title = str(finding.get("title", "Issue")).strip()
            body = str(finding.get("body", "")).strip()
            location = finding.get("code_location", {}) or {}
            abs_path = str(location.get("absolute_file_path", "")).strip()
            line_range = location.get("line_range", {}) or {}
            start_line = int(line_range.get("start", 0) or 0)
            end_line = int(line_range.get("end", start_line) or start_line)

            if not abs_path or start_line <= 0:
                continue

            # Convert absolute path to relative
            try:
                rel_path = str(Path(abs_path).resolve().relative_to(repo_root))
            except ValueError:
                rel_path = abs_path.lstrip("./")
            rel_path = rename_map.get(rel_path, rel_path)

            body_has_suggestion = has_suggestion(body)
            final_start = nearest_valid_line(rel_path, start_line)
            final_end = nearest_valid_line(rel_path, end_line)
            if final_start and final_end and final_start > final_end:
                final_start, final_end = final_end, final_start

            comment_body = f"{title}\n\n{body}"

            if self.config.dry_run:
                anchor = (
                    f"inline L{final_start}-{final_end}" if (final_start and final_end) else (
                        f"inline L{final_end or final_start}" if (final_end or final_start) else "file-level"
                    )
                )
                self._debug(1, f"DRY_RUN: would post {anchor} comment for {rel_path}:{start_line}-{end_line}")
                continue

            # Multi-line suggestion with explicit range when both endpoints are valid and likely same hunk
            if body_has_suggestion and final_start and final_end and final_start != final_end and both_in_same_hunk(rel_path, final_start, final_end):
                try:
                    url = f"{pr.url}/comments"
                    payload = {
                        "body": comment_body,
                        "commit_id": head_sha,
                        "path": rel_path,
                        "side": "RIGHT",
                        "start_line": int(final_start),
                        "line": int(final_end),
                    }
                    pr._requester.requestJsonAndCheck("POST", url, input=payload)
                    time.sleep(0.05)
                    continue
                except Exception as e:
                    self._debug(1, f"Multi-line suggestion post failed: {e}; falling back")

            # Single-line anchor fallback
            single_line = final_end or final_start
            if single_line:
                safe_body = comment_body if (not body_has_suggestion) else f"{title}\n\n{sanitize_suggestion_for_single_line(body)}"
                try:
                    url = f"{pr.url}/comments"
                    payload = {
                        "body": safe_body,
                        "commit_id": head_sha,
                        "path": rel_path,
                        "side": "RIGHT",
                        "line": int(single_line),
                    }
                    pr._requester.requestJsonAndCheck("POST", url, input=payload)
                    time.sleep(0.05)
                    continue
                except Exception as e:
                    self._debug(1, f"Single-line post failed: {e}; trying position-based fallback")
                    try:
                        commit_obj = repo.get_commit(head_sha)
                        pos = position_by_path.get(rel_path, {}).get(int(single_line), None)
                        if pos is not None:
                            pr.create_comment(safe_body, commit_obj, rel_path, pos)
                            time.sleep(0.05)
                            continue
                    except Exception as e2:
                        self._debug(1, f"Position-based fallback failed: {e2}")

            # Aggregate file-level fallback
            file_level_messages.setdefault(rel_path, []).append(
                self._format_file_level_fallback(rel_path, start_line, head_sha, title, body)
            )

        # Post file-level fallbacks as aggregated comments per file
        for rel_path, chunks in file_level_messages.items():
            text = "\n\n".join(chunks)
            try:
                commit_obj = repo.get_commit(head_sha)
                try:
                    pr.create_review_comment(body=text, commit=commit_obj, path=rel_path, subject_type="file")  # type: ignore[arg-type]
                except TypeError:
                    pr.as_issue().create_comment(text)
                time.sleep(0.1)
            except GithubException as e:
                self._debug(1, f"Failed file-level comment for {rel_path}: {e}")

    def _format_file_level_fallback(
        self, rel_path: str, start_line: int, head_sha: str, title: str, body: str
    ) -> str:
        repo_root = self.config.repo_root or Path(".").resolve()
        snippet = ""
        try:
            p = (repo_root / rel_path).resolve()
            if p.exists() and p.is_file():
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                idx = max(0, start_line - 1)
                window = lines[max(0, idx - 3) : min(len(lines), idx + 3)]
                # Re-number snippet lines relative to file for clarity
                base = max(1, start_line - 3)
                numbered = [f"{base + i:>6}: {ln}" for i, ln in enumerate(window)]
                snippet = "\n".join(numbered)
        except Exception:
            pass
        permalink = f"https://github.com/{self.config.owner}/{self.config.repo_name}/blob/{head_sha}/{rel_path}#L{start_line}"
        parts = [f"{title}", "", body, "", f"Permalink: {permalink}"]
        if snippet:
            parts.extend(["", "Context:", f"```\n{snippet}\n```"])
        parts.append("\n[fallback: not in diff]")
        return "\n".join(parts)
