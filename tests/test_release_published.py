from __future__ import annotations

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.parametrize(
    ("statuses", "attempts", "new_tag", "failure"),
    [
        ([], 1, "v1.9", None),
        ([422], 2, "v1.10", None),
        ([422] * 20, 20, None, "Exhausted attempts to create a unique v1.N tag."),
        ([500], 1, None, "Failed creating tag v1.9: API failure"),
    ],
    ids=["first-success", "collision-success", "exhausted", "unexpected-error"],
)
def test_release_promotion_only_mutates_release_after_tag_creation(
    statuses: list[int], attempts: int, new_tag: str | None, failure: str | None
) -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/release-published.yml"
    script = dedent(workflow.read_text(encoding="utf-8").split("          script: |\n", 1)[1])
    runner = """
        import { readFileSync } from 'node:fs';
        const { script, statuses } = JSON.parse(readFileSync(0, 'utf8'));
        const calls = [];
        const failures = [];
        const github = { rest: {
            git: {
                getRef: async () => ({ data: { object: { sha: 'commit-sha', type: 'commit' } } }),
                createRef: async ({ ref, sha }) => {
                    calls.push(['createRef', ref, sha]);
                    if (statuses.length) {
                        throw Object.assign(new Error('API failure'), { status: statuses.shift() });
                    }
                },
                deleteRef: async ({ ref }) => calls.push(['deleteRef', ref]),
                updateRef: async ({ ref, sha, force }) => calls.push(['updateRef', ref, sha, force]),
            },
            repos: {
                listTags: async () => ({ data: [{ name: 'v1.8' }] }),
                updateRelease: async ({ tag_name, name, draft, make_latest }) => {
                    calls.push(['updateRelease', tag_name, name, draft, make_latest]);
                },
            },
        } };
        const context = {
            repo: { owner: 'owner', repo: 'repo' },
            payload: { release: { tag_name: 'placeholder', id: 123 } },
        };
        const core = { info: () => {}, setFailed: (message) => failures.push(message) };
        const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
        await new AsyncFunction('github', 'context', 'core', script)(github, context, core);
        process.stdout.write(JSON.stringify({ calls, failures }));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", runner],
        input=json.dumps({"script": script, "statuses": statuses}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    expected_calls: list[list[str | bool]] = [
        ["createRef", f"refs/tags/v1.{9 + index}", "commit-sha"] for index in range(attempts)
    ]
    if new_tag is not None:
        expected_calls.extend(
            [
                ["updateRelease", new_tag, new_tag, False, "true"],
                ["deleteRef", "tags/placeholder"],
                ["updateRef", "tags/v1", "commit-sha", True],
                ["updateRef", "tags/latest", "commit-sha", True],
            ]
        )
    assert json.loads(result.stdout) == {
        "calls": expected_calls,
        "failures": [failure] if failure is not None else [],
    }
