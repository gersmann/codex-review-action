from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from cli.core.config import ReviewConfig
from cli.core.models import ReviewFinding, ReviewFindingLocation, ReviewRunResult
from cli.workflows.review_workflow import ReviewWorkflow


class FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def requestJsonAndCheck(self, method: str, url: str, input: dict | None = None):  # noqa: N802
        self.calls.append((method, url, input))
        return {}


class FakeIssueComment:
    def __init__(self, body: str, *, comment_id: int = 1) -> None:
        self.body = body
        self.id = comment_id
        self.deleted = False
        self.created_at = "now"
        self.user = None

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
    return ReviewConfig.from_args(
        github_token="t",
        repository="o/r",
        pr_number=1,
        openai_api_key="test-key",
        debug_level=0,
    )


def test_skips_summary_only_review_when_no_inline_comments(tmp_path: Path):
    config = make_config()
    rp = ReviewWorkflow(config)

    # No prior markers to avoid semantic dedup/model calls
    pr = FakePR(url="https://api.github.com/repos/o/r/pulls/1")

    result = ReviewRunResult.from_payload(
        {
            "findings": [],
            "overall_correctness": "patch is correct",
            "overall_explanation": "",
            "overall_confidence_score": None,
        }
    )

    # changed_files empty -> file_maps empty
    outcome = rp._post_results(
        result,
        changed_files=[],
        pr=cast(Any, pr),
        head_sha="deadbeef",
        rename_map={},
    )

    # Ensure we did not POST a review (no summary-only reviews allowed)
    assert len(pr._requester.calls) == 0
    assert outcome.as_dict() == {
        "total_findings": 0,
        "prefiltered_count": 0,
        "publishable_count": 0,
        "published_count": 0,
        "dropped_count": 0,
        "dry_run": False,
        "drop_reasons": "",
    }


def test_creates_bundled_review_with_inline_comment(tmp_path: Path):
    config = make_config()
    rp = ReviewWorkflow(config)

    pr = FakePR(url="https://api.github.com/repos/o/r/pulls/2")

    # Prepare a simple patch for sample.py with 3 added lines
    filename = "sample.py"
    patch = "@@ -0,0 +1,3 @@\n+foo\n+bar\n+baz\n"
    changed_files = [FakeChangedFile(filename, patch)]

    # Absolute path under current working directory
    abs_path = str((Path.cwd() / filename).resolve())

    result = ReviewRunResult.from_payload(
        {
            "overall_correctness": "patch is incorrect",
            "overall_explanation": "example",
            "overall_confidence_score": None,
            "findings": [
                {
                    "title": "Example finding",
                    "body": "Please adjust this line.",
                    "confidence_score": None,
                    "priority": None,
                    "code_location": {
                        "absolute_file_path": abs_path,
                        "line_range": {"start": 2, "end": 2},
                    },
                }
            ],
        }
    )

    outcome = rp._post_results(
        result,
        changed_files=cast(list[Any], changed_files),
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
    assert outcome.publishable_count == 1
    assert outcome.published_count == 1


def test_post_results_reports_dropped_findings(tmp_path: Path, capsys) -> None:
    config = make_config()
    rp = ReviewWorkflow(config)

    pr = FakePR(url="https://api.github.com/repos/o/r/pulls/3")

    outcome = rp._post_results(
        ReviewRunResult(
            overall_correctness="patch is incorrect",
            overall_explanation="example",
            overall_confidence_score=None,
            findings=[
                ReviewFinding(
                    title="Unknown file A",
                    body="",
                    confidence_score=None,
                    priority=None,
                    code_location=ReviewFindingLocation(
                        absolute_file_path=str((tmp_path / "unknown-a.py").resolve()),
                        start_line=1,
                        end_line=1,
                    ),
                ),
                ReviewFinding.from_mapping(
                    {
                        "title": "Unknown file B",
                        "body": "",
                        "confidence_score": None,
                        "priority": None,
                        "code_location": {
                            "absolute_file_path": str((tmp_path / "unknown-b.py").resolve()),
                            "line_range": {"start": 1, "end": 1},
                        },
                    }
                ),
            ],
        ),
        changed_files=[],
        pr=cast(Any, pr),
        head_sha="cafebabe",
        rename_map={},
    )

    out = capsys.readouterr().out
    assert "Posting dropped 2/2 findings before GitHub comment creation" in out
    assert "missing file map=2" in out
    assert outcome.dropped_count == 2
    assert outcome.describe_drops() == "missing file map=2"
