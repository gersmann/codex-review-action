from __future__ import annotations

from cli.review.dedupe import _extract_current_code_block


def test_extract_reads_hidden_evidence_marker() -> None:
    body = "The loop drops one retry.\n\n<!-- codex-current-code\nattempts += 1\n-->"

    assert _extract_current_code_block(body) == "attempts += 1"


def test_extract_reads_multiline_evidence_marker() -> None:
    body = "Paragraph.\n\n<!-- codex-current-code\nfoo\nbar\n-->"

    assert _extract_current_code_block(body) == "foo\nbar"


def test_extract_falls_back_to_legacy_current_code_block() -> None:
    body = "**Current code:**\n```python\nvalue = 2\n```\n\n**Problem:** broken."

    assert _extract_current_code_block(body) == "value = 2"


def test_extract_prefers_marker_over_legacy_block() -> None:
    body = "**Current code:**\n```python\nlegacy\n```\n\n<!-- codex-current-code\nmarker\n-->"

    assert _extract_current_code_block(body) == "marker"


def test_extract_returns_none_without_evidence() -> None:
    assert _extract_current_code_block("Just a matter-of-fact paragraph.") is None


def test_extract_decodes_escaped_comment_terminator() -> None:
    body = "Paragraph.\n\n<!-- codex-current-code\nif done: print('--&gt;')\n-->"

    assert _extract_current_code_block(body) == "if done: print('-->')"


def test_extract_decodes_escaped_ampersand_without_double_unescaping() -> None:
    body = "Paragraph.\n\n<!-- codex-current-code\na = b --&amp;gt; c\n-->"

    assert _extract_current_code_block(body) == "a = b --&gt; c"


def test_extract_preserves_anchored_whitespace() -> None:
    body = "Paragraph.\n\n<!-- codex-current-code\n    return foo\n-->"

    assert _extract_current_code_block(body) == "    return foo"


def test_current_code_matching_requires_exact_indentation(tmp_path) -> None:
    from cli.review.dedupe import _current_code_matches_file

    (tmp_path / "sample.py").write_text("def outer():\n    return foo\n", encoding="utf-8")

    assert _current_code_matches_file(tmp_path, "sample.py", "    return foo")
    assert not _current_code_matches_file(tmp_path, "sample.py", "        return foo")
