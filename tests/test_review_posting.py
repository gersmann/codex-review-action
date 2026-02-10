from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from cli.config import ReviewConfig
from cli.review_processor import ReviewProcessor


class FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def requestJsonAndCheck(self, method: str, url: str, input: dict | None = None):  # noqa: N802
        self.calls.append((method, url, input))
        return {}


class FakeIssueComment:
    def __init__(self, body: str) -> None:
        self.body = body
        self.deleted = False

    def delete(self) -> None:
        self.deleted = True


class FakeChangedFile:
    def __init__(self, filename: str, patch: str) -> None:
        self.filename = filename
        self.patch = patch


class FakePR:
    def __init__(self, url: str, issue_comments: list[FakeIssueComment] | None = None) -> None:
        self.url = url
        self._requester = FakeRequester()
        self._issue_comments = issue_comments or []

    # Methods used by the processor
    def get_issue_comments(self):
        return list(self._issue_comments)

    def get_review_comments(self):  # not used by these tests
        return []

    def get_reviews(
        self,
    ):  # used by _has_prior_codex_review; keep empty to avoid semantic dedup
        return []


def make_config() -> ReviewConfig:
    return ReviewConfig.from_args(github_token="t", repository="o/r", debug_level=0)


def test_skips_summary_only_review_when_no_inline_comments(tmp_path: Path):
    config = make_config()
    rp = ReviewProcessor(config)

    # No prior markers to avoid semantic dedup/model calls
    pr = FakePR(url="https://api.github.com/repos/o/r/pulls/1")

    result = {
        "findings": [],
        "overall_correctness": "patch is correct",
        "overall_explanation": "",
    }

    # changed_files empty -> file_maps empty
    rp._post_results(
        result,
        changed_files=[],
        repo=None,
        pr=cast(Any, pr),
        head_sha="deadbeef",
        rename_map={},
    )

    # Ensure we did not POST a review (no summary-only reviews allowed)
    assert len(pr._requester.calls) == 0


def test_creates_bundled_review_with_inline_comment(tmp_path: Path):
    config = make_config()
    rp = ReviewProcessor(config)

    pr = FakePR(url="https://api.github.com/repos/o/r/pulls/2")

    # Prepare a simple patch for sample.py with 3 added lines
    filename = "sample.py"
    patch = "@@ -0,0 +1,3 @@\n+foo\n+bar\n+baz\n"
    changed_files = [FakeChangedFile(filename, patch)]

    # Absolute path under current working directory
    abs_path = str((Path.cwd() / filename).resolve())

    result = {
        "overall_correctness": "patch is incorrect",
        "overall_explanation": "example",
        "findings": [
            {
                "title": "Example finding",
                "body": "Please adjust this line.",
                "code_location": {
                    "absolute_file_path": abs_path,
                    "line_range": {"start": 2, "end": 2},
                },
            }
        ],
    }

    rp._post_results(
        result,
        changed_files=cast(list[Any], changed_files),
        repo=None,
        pr=cast(Any, pr),
        head_sha="cafebabe",
        rename_map={},
    )

    # Exactly one POST to create a single review comment (no review wrapper)
    calls = pr._requester.calls
    assert len(calls) == 1
    method, url, payload = calls[0]
    assert method == "POST"
    assert url.endswith("/comments")
    assert isinstance(payload, dict)
    assert payload.get("commit_id") == "cafebabe"
    assert payload.get("path") == filename
