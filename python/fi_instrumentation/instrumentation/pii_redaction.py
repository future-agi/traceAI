"""
PII Redaction — lightweight regex-based scanning for common PII patterns.

Replaces detected PII with <ENTITY_TYPE> tokens (e.g., <EMAIL_ADDRESS>).
Zero external dependencies — uses only stdlib `re`.
"""

import re
from typing import Any

# ---------------------------------------------------------------------------
# Quick-check: a single regex that matches if the string *might* contain PII.
# If it doesn't match, we skip all 6 pattern scans entirely.
# ---------------------------------------------------------------------------
_QUICK_CHECK = re.compile(
    r"[A-Za-z0-9._%+\-]+@"  # email-like
    r"|\b\d{3}[\-\.\s]?\d{2}[\-\.\s]?\d{4}\b"  # SSN-like
    r"|\b(?:\d[ \-]*?){13,19}\b"  # credit-card-like
    r"|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"  # IPv4
    r"|(?:sk|pk)[-_](?:live|test|prod)[-_]"  # API key prefix
    r"|\(?\+?\d{1,4}\)?[\s\-\.]?\(?\d"  # phone-like
)

# ---------------------------------------------------------------------------
# Individual PII patterns — order matters (more specific first).
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

_SSN_RE = re.compile(
    r"\b\d{3}[\-\.\s]\d{2}[\-\.\s]\d{4}\b"
)

_CREDIT_CARD_RE = re.compile(
    r"\b(?:\d[ \-]*?){13,19}\b"
)

_PHONE_RE = re.compile(
    r"(?:\+?1[\s\-\.]?)?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}\b"
)

_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

_API_KEY_RE = re.compile(
    r"\b(?:sk|pk)[-_](?:live|test|prod)[-_][A-Za-z0-9]{20,}\b"
)

# Ordered: most specific → least specific to avoid partial overlaps.
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_EMAIL_RE, "<EMAIL_ADDRESS>"),
    (_SSN_RE, "<SSN>"),
    (_CREDIT_CARD_RE, "<CREDIT_CARD>"),
    (_API_KEY_RE, "<API_KEY>"),
    (_IP_RE, "<IP_ADDRESS>"),
    (_PHONE_RE, "<PHONE_NUMBER>"),
]


def redact_pii_in_string(text: str) -> str:
    """Scan *text* for PII patterns and replace each match with its entity token."""
    if not text or not _QUICK_CHECK.search(text):
        return text
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_pii_in_value(value: Any) -> Any:
    """Apply PII redaction to *value*.

    Handles:
    - ``str`` — scanned directly.
    - ``list`` of ``str`` — each element scanned.
    - Anything else — returned as-is.
    """
    if isinstance(value, str):
        return redact_pii_in_string(value)
    if isinstance(value, list):
        return [
            redact_pii_in_string(item) if isinstance(item, str) else item
            for item in value
        ]
    return value
