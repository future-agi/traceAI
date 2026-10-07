"""
PII Redaction — lightweight regex-based scanning for common PII patterns.

Replaces detected PII with <ENTITY_TYPE> tokens (e.g., <EMAIL_ADDRESS>).
Zero external dependencies — uses only stdlib `re`.
"""

import re
from typing import Any

from fi_instrumentation.fi_types import (
    DocumentAttributes,
    MessageAttributes,
    MessageContentAttributes,
    RerankerAttributes,
    SpanAttributes,
    ToolCallAttributes,
)

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

# Credit cards are handled separately from the simple sub() loop: a bare
# 13-19 digit run is only a *candidate* — it must also look grouped like a
# card and pass Luhn before it is redacted. UUIDs, timestamps, request ids
# and other incidental digit runs are not card-shaped (#195).
_CREDIT_CARD_CANDIDATE_RE = re.compile(
    r"\b(?:\d[ \-]*?){13,19}\b"
)

# Leading \b is load-bearing: without it the pattern matches the last
# 10-11 digits inside a longer run (timestamps, request ids), mangling
# numeric content the card check already cleared (#195).
_PHONE_RE = re.compile(
    r"\b(?:\+?1[\s\-\.]?)?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}\b"
)

_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

_API_KEY_RE = re.compile(
    r"\b(?:sk|pk)[-_](?:live|test|prod)[-_][A-Za-z0-9]{20,}\b"
)

# Ordered: most specific → least specific to avoid partial overlaps.
# The card check must run between SSN and API-key/phone: the phone pattern
# would otherwise claim the leading digits of a card-shaped run first.
_PII_PATTERNS_BEFORE_CARD: list[tuple[re.Pattern[str], str]] = [
    (_EMAIL_RE, "<EMAIL_ADDRESS>"),
    (_SSN_RE, "<SSN>"),
]
_PII_PATTERNS_AFTER_CARD: list[tuple[re.Pattern[str], str]] = [
    (_API_KEY_RE, "<API_KEY>"),
    (_IP_RE, "<IP_ADDRESS>"),
    (_PHONE_RE, "<PHONE_NUMBER>"),
]


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum over a string of ASCII digits."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_card_like(candidate: str) -> bool:
    """Whether a digit-run candidate is shaped like a real card number.

    A match must carry 13-19 digits and either be contiguous or use
    card-style grouping — groups of 4-6 digits separated by single
    spaces or dashes (Visa 4-4-4-4, Amex 4-6-5, Diners 4-6-4). The digit
    string must then pass the Luhn checksum every issuer applies.
    """
    digits = "".join(c for c in candidate if c.isdigit())
    if not 13 <= len(digits) <= 19:
        return False
    if " " in candidate or "-" in candidate:
        groups = candidate.strip().replace("-", " ").split()
        if any(not g.isdigit() or not 4 <= len(g) <= 6 for g in groups):
            return False
    return _luhn_ok(digits)


def _redact_credit_cards(text: str) -> str:
    """Replace only Luhn-valid, card-grouped candidates with <CREDIT_CARD>."""
    return _CREDIT_CARD_CANDIDATE_RE.sub(
        lambda m: "<CREDIT_CARD>" if _is_card_like(m.group(0)) else m.group(0),
        text,
    )


# ---------------------------------------------------------------------------
# Key scoping — PII redaction applies only to attribute keys that carry
# user/model free text. Structured identifiers (session.id, user.id,
# metadata) and typed scalars are never scanned: a regex hit there
# corrupts correlation identifiers, not secrets (#195).
# ---------------------------------------------------------------------------
_PII_SCANNABLE_KEYS = frozenset(
    {
        SpanAttributes.INPUT_VALUE,
        SpanAttributes.OUTPUT_VALUE,
        SpanAttributes.GEN_AI_TOOL_CALL_ARGUMENTS,
        SpanAttributes.GEN_AI_TOOL_CALL_RESULT,
        SpanAttributes.GEN_AI_RETRIEVAL_QUERY,
        SpanAttributes.GEN_AI_RERANKER_QUERY,
        RerankerAttributes.RERANKER_QUERY,
    }
)

# Nested free-text carriers keyed by their leaf attribute name — e.g.
# ``gen_ai.input.messages.0.message.content`` ends with ``.message.content``.
_PII_SCANNABLE_SUFFIXES = tuple(
    "." + leaf
    for leaf in (
        MessageAttributes.MESSAGE_CONTENT,
        MessageContentAttributes.MESSAGE_CONTENT_TEXT,
        DocumentAttributes.DOCUMENT_CONTENT,
        ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON,
    )
)


def key_carries_free_text(key: str) -> bool:
    """Whether *key* is an attribute that can hold free text and should be
    PII-scanned. Exact match for top-level content keys; suffix match for
    indexed carriers (``…0.message.content``, ``…0.document.content``)."""
    if key in _PII_SCANNABLE_KEYS:
        return True
    return any(key.endswith(suffix) for suffix in _PII_SCANNABLE_SUFFIXES)


def redact_pii_in_string(text: str) -> str:
    """Scan *text* for PII patterns and replace each match with its entity token."""
    if not text or not _QUICK_CHECK.search(text):
        return text
    for pattern, replacement in _PII_PATTERNS_BEFORE_CARD:
        text = pattern.sub(replacement, text)
    text = _redact_credit_cards(text)
    for pattern, replacement in _PII_PATTERNS_AFTER_CARD:
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
