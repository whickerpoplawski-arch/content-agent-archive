"""Scrub secrets and simple PII before a learning is stored.

Refusal leaves existing rows untouched: a rejected write is not an empty memory.
Episodic indexing can redact instead of refusing so one leaked token does not
drop the whole run from the index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Secrets are refused on explicit learn/edit. Patterns are intentionally narrow
# so ordinary copy ("3篇", "token 预算") is not treated as a credential.
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|secret|access[_-]?token|refresh[_-]?token|password|passwd)\b\s*[:=]\s*\S+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bsk-or-v1-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}={0,2}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)

_PII_PATTERNS = (
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[redacted-phone]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[redacted-id]"),
)

_PLACEHOLDER = re.compile(r"\[redacted[^\]]*\]")


class ScrubRejected(ValueError):
    """The text must not be stored. Existing memories stay as they are."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class ScrubResult:
    ok: bool
    text: str
    reason: str = ""
    redactions: int = 0


def _substantive(text: str) -> bool:
    stripped = _PLACEHOLDER.sub("", text or "")
    stripped = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    return len(stripped) >= 2


def _has_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_PATTERNS)


def _redact(text: str, patterns=_PII_PATTERNS) -> tuple[str, int]:
    count = 0
    out = text
    for pattern, token in patterns:
        out, n = pattern.subn(token, out)
        count += n
    return out, count


def _redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    out = text
    for pattern in _SECRET_PATTERNS:
        out, n = pattern.subn("[redacted-secret]", out)
        count += n
    return out, count


def scrub_memory_text(text: str, *, reject_secrets: bool = True) -> ScrubResult:
    """Clean one memory string.

    ``reject_secrets=True`` (learn/edit/feedback notes) refuses the write.
    ``reject_secrets=False`` (episodic titles/snippets) redacts and keeps the row.
    """
    raw = (text or "").strip()
    if not raw:
        return ScrubResult(ok=False, text="", reason="refused: empty memory text")
    if reject_secrets and _has_secret(raw):
        return ScrubResult(
            ok=False,
            text="",
            reason="refused: text looks like a secret; memory unchanged",
        )
    redactions = 0
    cleaned = raw
    if not reject_secrets:
        cleaned, n = _redact_secrets(cleaned)
        redactions += n
    cleaned, n = _redact(cleaned)
    redactions += n
    cleaned = cleaned.strip()
    if not _substantive(cleaned):
        return ScrubResult(
            ok=False,
            text="",
            reason="refused: nothing left after scrubbing",
            redactions=redactions,
        )
    return ScrubResult(ok=True, text=cleaned, redactions=redactions)


def require_clean_text(text: str) -> ScrubResult:
    """Scrub for a user-facing write. Raises ``ScrubRejected`` and stores nothing."""
    result = scrub_memory_text(text, reject_secrets=True)
    if not result.ok:
        raise ScrubRejected(result.reason or "refused")
    return result
