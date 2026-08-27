"""Formatting helpers shared by handlers, tools, and the CLI.

Deliberately dependency-free: the OpenClaw plugin used ``date-fns`` and
``libphonenumber-js``, but neither is worth a hard dependency here, so both
are reimplemented with the standard library.
"""

from __future__ import annotations

import json
import re

__all__ = [
    "clean_text_for_voice",
    "format_duration",
    "format_phone_number",
    "mask_phone",
    "normalize_phone",
    "slugify",
    "truncate",
]

_UNITS = (
    ("year", 365 * 24 * 3600),
    ("month", 30 * 24 * 3600),
    ("day", 24 * 3600),
    ("hour", 3600),
    ("minute", 60),
    ("second", 1),
)

# Markdown/formatting characters that TTS would read out literally.
_MD_CHARS = re.compile(r"[*_~`#>]")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_PARAGRAPH = re.compile(r"\n{2,}")
_MULTISPACE = re.compile(r"\s{2,}")
# Keep printable ASCII plus Latin-1 Supplement/Extended-A and Latin Extended
# Additional, so accented names survive but emoji and CJK are dropped.
_NON_SPEECH = re.compile("[^\u0020-\u007F\u00C0-\u024F\u1E00-\u1EFF]")

_TOOL_CALL_KEYS = ("name", "function", "tool_call", "arguments")


def format_duration(seconds: float | int | None) -> str:
    """Render a duration the way ``date-fns.formatDuration`` does.

    ``125`` -> ``"2 minutes 5 seconds"``.
    """
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return "0 seconds"

    parts: list[str] = []
    for label, size in _UNITS:
        count, total = divmod(total, size)
        if count:
            parts.append(f"{count} {label}" if count == 1 else f"{count} {label}s")
    return " ".join(parts) or "0 seconds"


def normalize_phone(phone: str) -> str:
    """Reduce a phone number to digits only - used for session keys."""
    return re.sub(r"\D", "", phone or "")


def format_phone_number(e164: str) -> str:
    """Render an E.164 number in a readable national-ish format.

    Falls back to the raw input for anything we do not recognise, which is
    the same contract the TypeScript version had.
    """
    raw = (e164 or "").strip()
    digits = normalize_phone(raw)

    if raw.startswith("+1") and len(digits) == 11:
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if not raw.startswith("+") and len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}"
    # Everything else stays as the caller gave it. Guessing a national format
    # without a phone-number library produces worse output than E.164, which
    # is at least unambiguous and dialable.
    return raw


def mask_phone(phone: str) -> str:
    """Partially redact a number for log output."""
    if not phone or len(phone) <= 6:
        return "***"
    return f"{phone[:6]}***"


def clean_text_for_voice(text: str) -> str:
    """Strip markdown, links, and emoji so TTS reads naturally.

    Also swallows raw JSON tool-call attempts, which sound like noise when
    spoken aloud.
    """
    if not text:
        return ""

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and any(key in parsed for key in _TOOL_CALL_KEYS):
            return "Done."

    out = _MD_LINK.sub(r"\1", text)
    out = _MD_CHARS.sub("", out)
    out = _PARAGRAPH.sub(". ", out)
    out = out.replace("\n", " ")
    out = _NON_SPEECH.sub("", out)
    out = _MULTISPACE.sub(" ", out)
    return out.strip()


def slugify(name: str) -> str:
    """URL-safe slug, byte-for-byte compatible with the OpenClaw plugin."""
    slug = re.sub(r"[^a-z0-9]", "-", (name or "").lower())
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def truncate(text: str, limit: int) -> str:
    """Hard-truncate with an ellipsis, never exceeding ``limit`` characters."""
    if text is None:
        return ""
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."
