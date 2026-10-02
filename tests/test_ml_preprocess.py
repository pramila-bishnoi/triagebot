"""Tests for text preprocessing shared by training and inference."""

from __future__ import annotations

from app.ml.preprocess import MAX_CHARS, build_text


def test_lowercases_and_collapses_whitespace():
    assert build_text("App  CRASHES", "on\n\n  Save") == "app crashes app crashes on save"


def test_title_counts_twice():
    assert build_text("hello", "") == "hello hello"


def test_none_and_empty_inputs():
    assert build_text(None, None) == ""
    assert build_text("", "") == ""
    assert build_text("title", None) == "title title"


def test_html_comments_removed():
    text = build_text("t", "<!-- Please describe the bug\nin detail -->real text")
    assert "describe" not in text
    assert "real text" in text


def test_code_blocks_replaced_by_token():
    text = build_text("t", "see:\n```python\nimport secret_module\nraise ValueError\n```\nend")
    assert "codeblock" in text
    assert "secret_module" not in text
    assert text.endswith("end")


def test_unclosed_code_fence_left_alone():
    assert "keep_me" in build_text("t", "```\nkeep_me")


def test_urls_replaced():
    text = build_text("t", "see https://example.com/a?b=c and http://x.y")
    assert "example.com" not in text
    assert text.count("urltoken") == 2


def test_length_capped():
    assert len(build_text("t", "word " * 5000)) == MAX_CHARS


def test_unicode_preserved():
    assert "überprüfung" in build_text("Überprüfung fehlgeschlagen", "")
