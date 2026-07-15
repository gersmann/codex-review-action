from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_composite_action_exposes_only_review_inputs_and_defaults() -> None:
    action_text = (ROOT / "action.yml").read_text(encoding="utf-8")

    assert action_text.startswith('name: "Codex Code Review"\n')
    assert "  mode:" not in action_text
    assert "${{ inputs.mode }}" not in action_text
    assert "CODEX_MODE:" not in action_text
    assert "review|act" not in action_text
    assert "CODEX_ACT_INSTRUCTIONS" not in action_text
    assert 'default: "gpt-5.6-luna"' in action_text
    assert 'default: "high"' not in action_text
    assert "xhigh for gpt-5.6-luna" in action_text
    assert "medium for gpt-5.6-terra and gpt-5.6-sol" in action_text
    assert (
        'PYTHONPATH="${{ github.action_path }}:${PYTHONPATH:-}" '
        "python3 -m cli.core.reasoning_effort"
    ) in action_text

    restore_step = action_text[action_text.index("    - name: Restore review Codex cache") :]
    restore_condition = restore_step.splitlines()[1]
    assert "github.event_name == 'pull_request'" in restore_condition

    save_step = action_text[action_text.index("    - name: Save review Codex cache") :]
    save_condition = save_step.splitlines()[1]
    assert "github.event_name == 'pull_request'" in save_condition


def test_self_review_workflow_routes_review_comments_without_write_access() -> None:
    workflow_text = (ROOT / ".github/workflows/codex-review.yml").read_text(encoding="utf-8")

    assert "contents: write" not in workflow_text
    workflow_triggers = workflow_text[: workflow_text.index("concurrency:")]
    assert "  pull_request:\n" not in workflow_triggers
    assert "comment-review:" in workflow_text
    assert "  review:\n" not in workflow_text
    assert "startsWith(github.event.comment.body, '/review ')" in workflow_text
    assert "startsWith(github.event.comment.body, '/verify ')" in workflow_text

    comment_job_index = workflow_text.index("  comment-review:")
    comment_job = workflow_text[comment_job_index:]
    guard_index = workflow_text.index("Verify same-repository PR", comment_job_index)
    checkout_index = workflow_text.index("actions/checkout@v5", guard_index)
    secret_index = workflow_text.index("openai_api_key:", guard_index)

    assert comment_job_index < guard_index < checkout_index < secret_index
    assert 'gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}"' in workflow_text
    assert '"$head_repository" != "$GITHUB_REPOSITORY"' in workflow_text
    assert "          mode:" not in comment_job
    assert "model: gpt-5.6-luna" in comment_job
    assert "reasoning_effort:" not in comment_job
    assert "web_search_mode: disabled" in comment_job
    assert "dry_run: 1" not in comment_job


def test_no_workflow_has_contents_write_permission() -> None:
    workflow_dir = ROOT / ".github/workflows"

    for workflow_path in workflow_dir.glob("*.yml"):
        workflow_text = workflow_path.read_text(encoding="utf-8")
        assert "contents: write" not in workflow_text, workflow_path.name


def test_github_actions_use_node_24_versions() -> None:
    action_text = (ROOT / "action.yml").read_text(encoding="utf-8")
    ci_text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    review_text = (ROOT / ".github/workflows/codex-review.yml").read_text(encoding="utf-8")
    all_actions = "\n".join((action_text, ci_text, readme_text, review_text))

    assert "actions/checkout@v5" in ci_text
    assert "actions/checkout@v5" in readme_text
    assert "actions/checkout@v5" in review_text
    assert "actions/setup-python@v6" in ci_text
    assert "astral-sh/setup-uv@v8.3.2" in ci_text
    assert "actions/cache/restore@v5" in action_text
    assert "actions/cache/save@v5" in action_text

    for node_20_action in (
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "astral-sh/setup-uv@v3",
        "actions/cache/restore@v4",
        "actions/cache/save@v4",
    ):
        assert node_20_action not in all_actions


def test_production_code_has_no_autonomous_editing_implementation() -> None:
    assert not (ROOT / "cli/workflows/edit_workflow.py").exists()
    assert not (ROOT / "cli/workflows/edit_prompt.py").exists()

    production_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "cli").rglob("*.py")
    )
    for forbidden in ("EditWorkflow", "act_instructions", "address comments"):
        assert forbidden not in production_text


def test_documentation_describes_only_on_demand_reviews() -> None:
    documentation = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            ROOT / "cli/README.md",
            ROOT / "pyproject.toml",
            ROOT / "AGENTS.md",
        )
    ).lower()

    for forbidden in (
        "--mode",
        "mode act",
        "autonomous edit",
        "edit_workflow",
        "edit_prompt",
        "codex_act_instructions",
    ):
        assert forbidden not in documentation

    assert "gpt-5.6-luna" in documentation
    assert "reasoning_effort: high" not in documentation
    assert "xhigh" in documentation
