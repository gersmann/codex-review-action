from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from github import Github
from github.GithubException import GithubException

from codex.config import ApprovalPolicy, CodexConfig, ReasoningEffort, ToolsConfig
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
            tools=ToolsConfig(web_search=True),
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

        # Extract PR data and changed files using PyGithub
        pr_data = getattr(pr, "raw_data", None)
        if not isinstance(pr_data, dict):
            pr_data = {
                "number": pr.number,
                "state": pr.state,
                "title": pr.title,
                "head": {
                    "sha": getattr(getattr(pr, "head", None), "sha", None),
                    "label": getattr(getattr(pr, "head", None), "label", ""),
                },
                "base": {
                    "sha": getattr(getattr(pr, "base", None), "sha", None),
                    "label": getattr(getattr(pr, "base", None), "label", ""),
                },
            }

        changed_files = list(pr.get_files())

        head_sha = getattr(getattr(pr, "head", None), "sha", None) or pr_data.get("head", {}).get(
            "sha"
        )
        if not head_sha:
            raise ValueError("Missing head commit SHA")

        self._debug(1, f"Changed files: {len(changed_files)}")
        for file in changed_files[:10]:  # Log first 10 files
            patch_len = (
                len((getattr(file, "patch", "") or "").splitlines())
                if getattr(file, "patch", None)
                else 0
            )
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

        # Parse JSON response
        try:
            result = json.loads(output)
        except json.JSONDecodeError as e:
            print("Model did not return valid JSON:")
            print(output)
            raise CodexExecutionError(f"JSON parsing error: {e}") from e

        # Process and post results
        self._post_results(result, changed_files, repo, pr, head_sha)

        return result

    def _write_context_artifacts(self, pr, changed_files: list) -> None:
        """Create a `.codex-context` directory with diffs and PR context (including comments)."""
        repo_root = self.config.repo_root or Path(".").resolve()
        base_dir_name = getattr(self.config, "context_dir_name", ".codex-context") or ".codex-context"
        base_dir = (repo_root / base_dir_name).resolve()

        diffs_dir = base_dir / "diffs"
        diffs_dir.mkdir(parents=True, exist_ok=True)

        # Write combined diffs file and per-file patches
        combined_lines: list[str] = []
        for file in changed_files:
            filename = getattr(file, "filename", None)
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

        (base_dir / "combined_diffs.txt").write_text("\n" + ("\n" + ("-" * 80) + "\n").join(combined_lines), encoding="utf-8")

        # Write PR metadata and comments into pr.md
        parts: list[str] = []
        parts.append(f"PR #{getattr(pr, 'number', '')}: {getattr(pr, 'title', '')}")
        parts.append("")
        parts.append(f"URL: {getattr(pr, 'html_url', '')}")
        parts.append(f"Author: {getattr(getattr(pr, 'user', None), 'login', '')}")
        parts.append(f"State: {getattr(pr, 'state', '')}")
        parts.append("")
        body = getattr(pr, "body", "") or ""
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
                author = getattr(getattr(c, "user", None), "login", "")
                created = getattr(c, "created_at", "")
                parts.append(f"- [{created}] @{author}:\n{getattr(c, 'body', '')}\n")

        # Review comments (inline on diffs)
        try:
            review_comments = list(pr.get_review_comments())
        except Exception:
            review_comments = []
        if review_comments:
            parts.append("Review Comments:")
            for rc in review_comments:
                author = getattr(getattr(rc, "user", None), "login", "")
                created = getattr(rc, "created_at", "")
                path = getattr(rc, "path", "")
                line = getattr(rc, "line", None) or getattr(rc, "original_line", None)
                parts.append(f"- [{created}] @{author} on {path}:{line}\n{getattr(rc, 'body', '')}\n")

        (base_dir / "pr.md").write_text("\n".join(parts) + "\n", encoding="utf-8")

    def _post_results(
        self,
        result: dict[str, Any],
        changed_files: list,
        repo,
        pr,
        head_sha: str,
    ) -> None:
        """Post review results to GitHub."""
        findings: list[dict[str, Any]] = list(result.get("findings", []) or [])
        overall = str(result.get("overall_correctness", "")).strip() or "patch is correct"
        overall_explanation = str(result.get("overall_explanation", "")).strip()
        overall_conf = result.get("overall_confidence_score")

        # Compose summary
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

        # If this PR already has a Codex review, deduplicate new findings using the fast model
        try:
            if self._has_prior_codex_review(pr):
                existing = self._collect_existing_comment_texts(pr)
                filtered = self._deduplicate_findings(findings, existing)
                if isinstance(filtered, list):
                    if len(filtered) != len(findings):
                        print(f"Dedup kept {len(filtered)}/{len(findings)} findings (fast model)")
                    findings = filtered
        except Exception as e:
            self._debug(1, f"Deduplication step failed: {e}")

        # Post review summary
        try:
            if self.config.dry_run:
                self._debug(1, "DRY_RUN: would create PR review")
            else:
                pr.create_review(body=summary, event="COMMENT")
        except GithubException as e:
            print(f"Failed to post review summary: {e}", file=sys.stderr)

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

        # Post individual findings
        self._post_findings(findings, position_by_path, repo, pr, head_sha)

    def _has_prior_codex_review(self, pr) -> bool:
        try:
            for rev in pr.get_reviews():
                body = getattr(rev, "body", "") or ""
                if "Codex Autonomous Review:" in body:
                    return True
        except Exception:
            pass
        # Also check issue comments, in case summary was posted there in previous versions
        try:
            for c in pr.get_issue_comments():
                if "Codex Autonomous Review:" in (getattr(c, "body", "") or ""):
                    return True
        except Exception:
            pass
        return False

    def _collect_existing_comment_texts(self, pr) -> list[str]:
        texts: list[str] = []
        try:
            for c in pr.get_issue_comments():
                t = (getattr(c, "body", "") or "").strip()
                if t:
                    texts.append(t)
        except Exception:
            pass
        try:
            for rc in pr.get_review_comments():
                t = (getattr(rc, "body", "") or "").strip()
                if t:
                    texts.append(t)
        except Exception:
            pass
        return texts

    def _deduplicate_findings(self, findings: list[dict[str, Any]], existing_comments: list[str]) -> list[dict[str, Any]]:
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
            "Given `new_findings` and `existing_comments`, return JSON {\"keep\": [indices]} where indices refer to new_findings[index].\n"
            "Consider a new finding a duplicate if an existing comment already conveys the same issue for the same file and nearby lines,\n"
            "or if it is semantically redundant. Prefer recall (keep) when unsure.\n"
        )

        payload = {
            "new_findings": compact_findings,
            "existing_comments": existing_comments[:200],  # cap to avoid huge prompts
        }

        prompt = instructions + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False) + "\n\nOUTPUT: JSON with only the 'keep' array."

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
            keep_set = {int(i) for i in keep if isinstance(i, int) or (isinstance(i, str) and str(i).isdigit())}
            return [f for i, f in enumerate(findings) if i in keep_set]
        except Exception:
            return findings

    def _post_findings(
        self,
        findings: list[dict[str, Any]],
        position_by_path: dict[str, dict[int, int]],
        repo,
        pr,
        head_sha: str,
    ) -> None:
        """Post individual findings as comments using PyGithub."""
        repo_root = self.config.repo_root or Path(".").resolve()

        for finding in findings:
            title = str(finding.get("title", "Issue")).strip()
            body = str(finding.get("body", "")).strip()
            location = finding.get("code_location", {}) or {}
            abs_path = str(location.get("absolute_file_path", "")).strip()
            line_range = location.get("line_range", {}) or {}
            start_line = int(line_range.get("start", 0))

            if not abs_path or start_line <= 0:
                continue

            # Convert absolute path to relative
            try:
                rel_path = str(Path(abs_path).resolve().relative_to(repo_root))
            except ValueError:
                rel_path = abs_path.lstrip("./")

            # Check if we can anchor the comment
            pos_map = position_by_path.get(rel_path, {})
            position = pos_map.get(start_line)
            can_anchor = position is not None

            comment_body = f"{title}\n\n{body}"

            try:
                if self.config.dry_run:
                    action = "inline" if can_anchor and position is not None else "file-level"
                    self._debug(1, f"DRY_RUN: would post {action} comment for {rel_path}:{start_line}")
                    continue
                if can_anchor and position is not None:
                    self._debug(
                        1,
                        f"Posting inline comment: {rel_path}:{start_line} -> position={position}",
                    )
                    commit_obj = repo.get_commit(head_sha)
                    pr.create_comment(comment_body, commit_obj, rel_path, position)
                else:
                    self._debug(
                        1,
                        f"Posting file-level comment: {rel_path} (line {start_line} not in diff)",
                    )
                    commit_obj = repo.get_commit(head_sha)
                    try:
                        pr.create_review_comment(
                            body=comment_body
                            + "\n\n(Note: referenced line not in diff; posting at file level.)",
                            commit=commit_obj,
                            path=rel_path,
                            subject_type="file",
                        )  # type: ignore[arg-type]
                    except TypeError:
                        pr.as_issue().create_comment(
                            f"[File: {rel_path}]\n\n{comment_body}\n\n(Note: referenced line not in diff; posting at file level.)"
                            )
                # Small delay to avoid rate limiting
                time.sleep(0.2)

            except GithubException as e:
                print(f"Failed to post comment for {rel_path}:{start_line}: {e}", file=sys.stderr)
