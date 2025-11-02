from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from codex.config import SandboxMode
from github import Github
from github.File import File
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest
from github.PullRequestComment import PullRequestComment
from github.Repository import Repository

from .anchor_engine import build_anchor_maps, resolve_range
from .codex_client import CodexClient
from .config import ReviewConfig
from .context_manager import ContextManager
from .edit_processor import EditProcessor
from .exceptions import CodexExecutionError
from .prompt_builder import PromptBuilder


class ReviewProcessor:
    """Main processor for code review operations (PyGithub-based)."""

    def __init__(self, config: ReviewConfig) -> None:
        self.config = config
        self.prompt_builder = PromptBuilder(config)
        self.codex_client = CodexClient(config)
        self.context_manager = ContextManager()
        self.edit_processor = EditProcessor(config)
        self._gh: Github | None = None

    def _debug(self, level: int, message: str) -> None:
        if self.config.debug_level >= level:
            print(f"[debug{level}] {message}", file=sys.stderr)

    def _get_github_client(self) -> Github:
        """Return a cached Github client, creating it on first use."""
        if self._gh is None:
            self._gh = Github(login_or_token=self.config.github_token, per_page=100)
        return self._gh

    def _build_review_base_instructions(self, guidelines: str) -> str:
        """Construct base instructions for Codex review runs."""

        parts: list[str] = [
            "You are an autonomous code review assistant.",
            "Follow the review guidelines below verbatim while producing prioritized, actionable findings.",
        ]

        guidelines_text = guidelines.strip()
        if guidelines_text:
            parts.append("\nReview guidelines:\n" + guidelines_text)

        extra = (self.config.additional_prompt or "").strip()
        if extra:
            parts.append("\nAdditional instructions:\n" + extra)

        parts.append(
            "Use git commands as needed to inspect the diff between the PR head and the base branch."
        )

        return "\n".join(parts).strip()

    def process_review(self, pr_number: int | None = None) -> dict[str, Any]:
        """Process a code review for the given pull request."""
        if pr_number is None:
            pr_number = self.config.pr_number
        if pr_number is None:
            raise ValueError("PR number must be provided")

        self._debug(1, f"Processing review for {self.config.repository} PR #{pr_number}")

        # Initialize PyGithub client and fetch PR
        repo = self._get_github_client().get_repo(f"{self.config.owner}/{self.config.repo_name}")
        pr = repo.get_pull(pr_number)

        # Validate PR object
        if not isinstance(pr, PullRequest):
            raise ValueError("Expected a PullRequest instance")

        changed_files = list(pr.get_files())
        # Map old->new paths for renamed files so we can anchor against HEAD paths
        rename_map: dict[str, str] = {}
        for f in changed_files:
            if f.status == "renamed":
                prev = f.previous_filename
                if prev:
                    rename_map[prev] = f.filename

        head_sha = pr.head.sha if pr.head else None
        if not head_sha:
            raise ValueError("Missing head commit SHA")

        self._debug(1, f"Changed files: {len(changed_files)}")
        for cf in changed_files[:10]:  # Log first 10 files
            # Guard against files where GitHub omits the patch (e.g., binary or large files)
            patch_len = len(cf.patch.splitlines()) if isinstance(cf.patch, str) else 0
            self._debug(
                2,
                f" - {cf.filename} status={cf.status} patch_len={patch_len}",
            )

        # Prepare local context artifacts (diffs + PR contents with comments)
        repo_root = self.config.repo_root or Path(".").resolve()
        context_dir_name = self.config.context_dir_name or ".codex-context"
        self.context_manager.write_context_artifacts(pr, repo_root, context_dir_name)

        # Load guidelines and compose prompt
        guidelines = self.prompt_builder.load_guidelines()
        prompt = self.prompt_builder.compose_prompt(changed_files, pr)

        base_instructions = self._build_review_base_instructions(guidelines)

        self._debug(2, f"Prompt length: {len(prompt)} chars")
        print("Running Codex to generate review findings...", flush=True)

        # Execute Codex with limited retries if JSON parsing fails
        max_attempts = 3  # initial try + up to two retries
        last_error: json.JSONDecodeError | None = None
        result: dict[str, Any] | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self._debug(
                    1,
                    f"Retrying Codex due to invalid JSON (attempt {attempt}/{max_attempts})",
                )

            # Execute Codex
            output = self.codex_client.execute(
                prompt,
                config_overrides={
                    "base_instructions": base_instructions,
                    # Skip external sandbox: allow git commands without codex-linux-sandbox
                    # this is in CI, so no interactions with a real environment in the first place.
                    # necessary for CI now as long as the linux sandbox is not available.
                    "sandbox_mode": SandboxMode.DANGER_FULL_ACCESS,
                },
            )

            # Parse JSON response (robust to fenced or prefixed/suffixed text)
            try:
                result = self.codex_client.parse_json_response(output)
                break
            except json.JSONDecodeError as e:
                last_error = e
                if attempt == max_attempts:
                    print("Model did not return valid JSON:")
                    print(output)
                continue

        if result is None:
            raise CodexExecutionError(
                f"JSON parsing error after {max_attempts} attempts: {last_error}"
            ) from last_error

        # Compose and post a fresh timeline summary as an issue comment
        findings_for_summary = list(result.get("findings", []) or [])
        summary_lines = [
            "Codex Autonomous Review:",
            f"- Overall: {str(result.get('overall_correctness', '') or '').strip() or 'patch is correct'}",
            f"- Findings (total): {len(findings_for_summary)}",
        ]
        overall_explanation = str(result.get("overall_explanation", "")).strip()
        if overall_explanation:
            summary_lines.append("")
            summary_lines.append(overall_explanation)
        summary_lines.append("")
        summary_lines.append(
            'Tip: comment with "/codex address comments" to attempt automated fixes for unresolved review threads.'
        )
        summary = "\n".join(summary_lines)

        if not self.config.dry_run:
            self._delete_prior_summary(pr)
            # must fail if comment creation fails
            pr.as_issue().create_comment(summary)
        else:
            self._debug(1, "DRY_RUN: would refresh summary issue comment")

        # Process and post inline findings as code comments
        self._post_results(result, changed_files, repo, pr, head_sha, rename_map)

        return result

    def _post_results(
        self,
        result: dict[str, Any],
        changed_files: list[File],
        repo: Repository | Any,
        pr: PullRequest,
        head_sha: str,
        rename_map: dict[str, str],
    ) -> None:
        """Post review results to GitHub."""
        findings: list[dict[str, Any]] = list(result.get("findings", []) or [])
        if self._has_prior_codex_review(pr):
            # 1) Strict prefilter by file/line proximity to avoid reposting
            #    even when prior threads are marked resolved.
            existing_struct = self._collect_existing_review_comments(pr)
            findings = self._prefilter_duplicates_by_location(findings, existing_struct, rename_map)

            # 2) Semantic dedupe with fast model for remaining near-duplicates
            existing = self._collect_existing_comment_texts(pr)
            filtered = self._deduplicate_findings(findings, existing)
            if isinstance(filtered, list):
                print(f"Dedup kept {len(filtered)}/{len(findings)} findings (fast model)")
                findings = filtered

        # Compute anchors and post inline findings

        # Build anchor maps for inline comments (deterministic)
        file_maps = build_anchor_maps(changed_files)

        # Persist anchor maps for debugging and line mapping inspection
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
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "anchor_maps.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

        # Post findings bundled as a single PR review with inline comments
        self._post_findings(findings, file_maps, repo, pr, head_sha, rename_map)

    def _delete_prior_summary(self, pr: PullRequest) -> None:
        """Delete prior Codex summary issue comments.

        Note: We intentionally do not attempt to mutate or dismiss prior PR
        reviews here. GitHub does not support editing a submitted review body
        and dismissals do not remove comment-only reviews. Our posting logic
        avoids creating summary-only reviews entirely to prevent duplication.
        """
        marker = "Codex Autonomous Review:"
        # Issue comments
        comments = list(pr.get_issue_comments())

        for c in comments:
            body_raw = c.body or ""
            body = body_raw.strip()
            if marker not in body:
                continue
            try:
                c.delete()
                self._debug(1, f"Deleted prior summary issue comment id={c.id}")
            except Exception as e:
                self._debug(1, f"Failed to delete prior summary issue comment id={c.id}: {e}")

    def _has_prior_codex_review(self, pr: PullRequest) -> bool:
        reviews = list(pr.get_reviews())
        for rev in reviews:
            if isinstance(rev.body, str) and "Codex Autonomous Review:" in rev.body:
                return True
        # Also check issue comments, in case summary was posted there in previous versions
        comments = list(pr.get_issue_comments())
        for c in comments:
            if isinstance(c, IssueComment) and "Codex Autonomous Review:" in (c.body or ""):
                return True
        return False

    def _collect_existing_comment_texts(self, pr: PullRequest) -> list[str]:
        """Collect only file/diff review comments for deduplication.

        Excludes PR-level summaries and issue comments so they don't suppress
        per-file findings.
        """
        texts: list[str] = []
        comments = list(pr.get_review_comments())
        for rc in comments:
            if isinstance(rc, PullRequestComment):
                body = rc.body.strip()
                path = rc.path
                line = rc.line or rc.original_line
                loc = f"{path}:{line}" if path and line else path
                prefix = f"[{loc}] " if loc else ""
                texts.append(prefix + body)
        return texts

    def _collect_existing_review_comments(self, pr: PullRequest) -> list[dict[str, Any]]:
        """Collect structured inline review comments (path, line, body).

        Note: We intentionally do not distinguish resolved vs. unresolved here.
        Any prior inline comment acts as a suppressor to prevent reposting,
        which also covers resolved threads.
        """
        items: list[dict[str, Any]] = []
        comments = list(pr.get_review_comments())
        for rc in comments:
            if isinstance(rc, PullRequestComment):
                body = rc.body.strip()
                path = rc.path
                line = rc.line or rc.original_line
                if body and path and isinstance(line, int):
                    items.append({"path": path, "line": int(line), "body": body})
        return items

    def _prefilter_duplicates_by_location(
        self,
        findings: list[dict[str, Any]],
        existing: list[dict[str, Any]],
        rename_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Drop findings that match an existing inline comment by file and nearby lines.

        A finding is considered a duplicate if there exists an inline comment with:
        - same repo-relative `path` (after applying rename_map), and
        - `line` within a small window of the finding's start/end lines.
        """
        repo_root = self.config.repo_root or Path(".").resolve()

        index: dict[str, set[int]] = {}
        for item in existing:
            p = rename_map.get(item.get("path", ""), item.get("path", ""))
            if not p:
                continue
            index.setdefault(p, set()).add(int(item.get("line", 0) or 0))

        if not index:
            return findings

        def to_rel(abs_path: str) -> str:
            try:
                return str(Path(abs_path).resolve().relative_to(repo_root))
            except Exception:
                return abs_path.lstrip("./")

        WINDOW = 3  # lines of tolerance
        filtered: list[dict[str, Any]] = []
        for f in findings:
            abs_path, start, end = self._extract_finding_location(f)
            rel_path = rename_map.get(to_rel(abs_path), to_rel(abs_path)) if abs_path else ""

            if not rel_path or start <= 0:
                filtered.append(f)
                continue

            lines = index.get(rel_path)
            if not lines:
                filtered.append(f)
                continue

            is_dup = any(abs(L - start) <= WINDOW or abs(L - end) <= WINDOW for L in lines if L > 0)
            if not is_dup:
                filtered.append(f)

        dropped = len(findings) - len(filtered)
        if dropped > 0:
            print(f"Prefilter dropped {dropped}/{len(findings)} findings due to existing comments")
        return filtered

    def _deduplicate_findings(
        self, findings: list[dict[str, Any]], existing_comments: list[str]
    ) -> list[dict[str, Any]]:
        """Use the fast model to filter out findings already covered by existing comments."""
        # Build compact payload
        compact_findings: list[dict[str, Any]] = []
        for idx, f in enumerate(findings):
            abs_path, start, _ = self._extract_finding_location(f)
            compact_findings.append(
                {
                    "index": idx,
                    "title": str(f.get("title", "")),
                    "body": str(f.get("body", "")),
                    "path": abs_path,
                    "start": start,
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

        raw = self.codex_client.execute(
            prompt,
            model_name=self.config.fast_model_name,
            reasoning_effort=self.config.fast_reasoning_effort,
            suppress_stream=True,
            config_overrides={"sandbox_mode": SandboxMode.DANGER_FULL_ACCESS},
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
        file_maps: dict[str, Any],
        repo: Repository | Any,
        pr: PullRequest,
        head_sha: str,
        rename_map: dict[str, str],
    ) -> None:
        """Post findings as a bundled PR review with inline comments."""
        repo_root = self.config.repo_root or Path(".").resolve()

        def to_rel(p: str) -> str:
            try:
                return str(Path(p).resolve().relative_to(repo_root))
            except Exception:
                return p.lstrip("./")

        review_comments: list[dict[str, Any]] = []
        for finding in findings:
            title = str(finding.get("title", "Issue")).strip()
            body = str(finding.get("body", "")).strip()
            abs_path, start_line, end_line = self._extract_finding_location(finding)
            if not abs_path or start_line <= 0:
                continue

            rel_path = rename_map.get(to_rel(abs_path), to_rel(abs_path))
            fmap = file_maps.get(rel_path)
            if not fmap:
                continue

            has_suggestion = "```suggestion" in body
            anchor = resolve_range(rel_path, start_line, end_line, has_suggestion, fmap)
            if not anchor:
                if self.config.dry_run:
                    self._debug(
                        1,
                        f"DRY_RUN: would skip (no anchor) for {rel_path}:{start_line}-{end_line}",
                    )
                continue

            final_body = body
            if has_suggestion and not (
                anchor.get("allow_suggestion") and anchor.get("kind") == "range"
            ):
                final_body = body.replace("```suggestion", "```diff")

            comment_body = f"{title}\n\n{final_body}" if final_body else title
            payload: dict[str, Any] = {
                "body": comment_body,
                "path": rel_path,
                "side": "RIGHT",
            }
            if anchor["kind"] == "range":
                payload["start_line"] = int(anchor["start_line"])
                payload["start_side"] = "RIGHT"
                payload["line"] = int(anchor["end_line"])
            else:
                payload["line"] = int(anchor["line"])

            review_comments.append(payload)

        if not review_comments:
            if self.config.dry_run:
                self._debug(1, "DRY_RUN: no inline findings to post")
            return

        # Post each finding as a standalone review comment (no PR review wrapper)
        for payload in review_comments:
            # Build a clean payload for the single-comment API
            single: dict[str, Any] = {
                "body": payload["body"],
                "path": payload["path"],
                "side": payload.get("side", "RIGHT"),
                "commit_id": head_sha,
            }
            if "start_line" in payload:
                # Range comment requires both start_line and start_side
                single["start_line"] = int(payload["start_line"])  # range comment
                single["start_side"] = str(payload.get("start_side", "RIGHT"))
                single["line"] = int(payload["line"])
            else:
                single["line"] = int(payload["line"])  # single-line comment
            if self.config.dry_run:
                self._debug(
                    1,
                    f"DRY_RUN: would POST /comments for {single.get('path')}:{single.get('line')}",
                )
                continue
            pr._requester.requestJsonAndCheck("POST", f"{pr.url}/comments", input=single)

    @staticmethod
    def _extract_finding_location(finding: dict[str, Any]) -> tuple[str, int, int]:
        """Return (abs_path, start_line, end_line) from a finding dict; defaults to ("",0,0)."""
        loc = finding.get("code_location")
        if not isinstance(loc, dict):
            return "", 0, 0
        abs_path = str(loc.get("absolute_file_path") or "").strip()
        rng = loc.get("line_range")
        if not isinstance(rng, dict):
            return abs_path, 0, 0

        def as_int(x: Any, default: int = 0) -> int:
            try:
                return int(x)
            except (TypeError, ValueError):
                return default

        start = as_int(rng.get("start"), 0)
        end = as_int(rng.get("end"), start)
        if end <= 0 and start > 0:
            end = start
        return abs_path, start, end
