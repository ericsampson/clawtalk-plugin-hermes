"""Formatting helpers - the parts other modules depend on being exact."""

from __future__ import annotations

import pytest

from clawtalk.formatting import (
    clean_text_for_voice,
    format_duration,
    format_phone_number,
    mask_phone,
    normalize_phone,
    slugify,
    truncate,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 seconds"),
        (-5, "0 seconds"),
        (None, "0 seconds"),
        (1, "1 second"),
        (45, "45 seconds"),
        (60, "1 minute"),
        (125, "2 minutes 5 seconds"),
        (3661, "1 hour 1 minute 1 second"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+15551234567", "(555) 123-4567"),
        ("5551234567", "(555) 123-4567"),
        ("+353851234567", "+353851234567"),  # non-NANP: left as dialable E.164
        ("", ""),
        ("not-a-number", "not-a-number"),
    ],
)
def test_format_phone_number(raw, expected):
    assert format_phone_number(raw) == expected


def test_normalize_and_mask_phone():
    assert normalize_phone("+1 (555) 123-4567") == "15551234567"
    assert mask_phone("+15551234567") == "+15551***"
    assert mask_phone("+1234") == "***"
    assert mask_phone("") == "***"


class TestCleanTextForVoice:
    def test_strips_markdown_and_links(self):
        text = "**Bold** and _italic_ and [a link](https://example.com)"
        assert clean_text_for_voice(text) == "Bold and italic and a link"

    def test_collapses_newlines(self):
        assert clean_text_for_voice("One\n\nTwo\nThree") == "One. Two Three"

    def test_drops_emoji_but_keeps_accents(self):
        assert clean_text_for_voice("Ready 🚀 for Zoë") == "Ready for Zoë"

    def test_swallows_raw_tool_call_json(self):
        # A model that emits a tool call as text would otherwise have the raw
        # JSON read aloud to the caller.
        raw = '{"name": "send_slack", "arguments": {"channel": "#general"}}'
        assert clean_text_for_voice(raw) == "Done."

    def test_keeps_ordinary_json_looking_text(self):
        assert clean_text_for_voice('{"total": 42}') == '{"total": 42}'

    def test_empty(self):
        assert clean_text_for_voice("") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Call Alice about the quote", "call-alice-about-the-quote"),
        ("  Mixed CASE!! ", "mixed-case"),
        ("---", ""),
        ("", ""),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_truncate():
    assert truncate("hello", 10) == "hello"
    assert truncate("hello world", 8) == "hello..."
    assert len(truncate("x" * 500, 300)) == 300
    assert truncate("", 10) == ""
