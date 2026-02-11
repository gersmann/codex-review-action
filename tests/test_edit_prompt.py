from __future__ import annotations

from cli.config import ReviewConfig
from cli.edit_prompt import build_edit_prompt


class _Ref:
    def __init__(self, ref: str) -> None:
        self.ref = ref


class _PR:
    def __init__(self) -> None:
        self.head = _Ref("feature/test-branch")
        self.base = _Ref("main")


def test_build_edit_prompt_includes_commit_push_completion_rules() -> None:
    config = ReviewConfig(
        github_token="token",
        repository="owner/repo",
        mode="act",
    )
    prompt = build_edit_prompt(
        config=config,
        command_text="/codex update README wording",
        pr=_PR(),
        comment_ctx=None,
        unresolved_block="",
    )

    assert "<completion_rules>" in prompt
    assert "If code changes were performed, commit those changes and push the branch." in prompt
    assert "If no code changes were needed, do not create an empty commit." in prompt
