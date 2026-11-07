"""
PII Redaction — lightweight scanning for common PII patterns.

Replaces detected PII with <ENTITY_TYPE> tokens (e.g., <EMAIL_ADDRESS>).
Zero external dependencies — uses only stdlib `re`.
"""

import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Quick-check: a single regex that matches if the string *might* contain PII.
# If it doesn't match and there is no @, skip all PII passes entirely.
# ---------------------------------------------------------------------------
_QUICK_CHECK = re.compile(
    r"\b\d{3}[\-\.\s]?\d{2}[\-\.\s]?\d{4}\b"  # SSN-like
    r"|\b(?:\d[ \-]*?){13,19}\b"  # credit-card-like
    r"|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"  # IPv4
    r"|(?:sk|pk)[-_](?:live|test|prod)[-_]"  # API key prefix
    r"|\(?\+?\d{1,4}\)?[\s\-\.]?\(?\d"  # phone-like
)

# ---------------------------------------------------------------------------
# Email uses a forward scan instead of unbounded local-part regex retries.
# These are the exact ASCII classes of the former email expression.
# ---------------------------------------------------------------------------
_EMAIL_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_EMAIL_DOMAIN_CHARS = _EMAIL_LETTERS | frozenset("0123456789.-")
_EMAIL_LOCAL_CHARS = _EMAIL_DOMAIN_CHARS | frozenset("_%+")


def _is_word_character(char: str) -> bool:
    # Python's Unicode regex word class is alphanumeric characters plus _.
    return char == "_" or char.isalnum()


def _email_match_end(text: str, domain_start: int) -> Optional[int]:
    """Find the greedy domain/TLD end in one contiguous domain-character run."""
    match_end = None
    after_dot = False
    tld_length = 0
    index = domain_start
    while index < len(text):
        char = text[index]
        if char not in _EMAIL_DOMAIN_CHARS:
            break
        if char == ".":
            # The domain prefix before the separating dot must be nonempty.
            after_dot = index > domain_start
            tld_length = 0
        elif after_dot and char in _EMAIL_LETTERS:
            tld_length += 1
            end = index + 1
            if tld_length >= 2 and (
                end == len(text) or not _is_word_character(text[end])
            ):
                # A later valid dot wins, just as the greedy domain prefix did.
                match_end = end
        else:
            after_dot = False
            tld_length = 0
        index += 1
    return match_end


def _redact_email_addresses(text: str) -> str:
    """Preserve the old email expression's matches with O(len(text)) work.

    The outer cursor never retreats. Each local run retains only its earliest
    word-boundary start. Domain lookaheads for different @ characters cannot
    overlap: @ itself is not a domain character. Thus even failed matches and
    multiple @ characters cause only a constant number of reads per character.
    Unmatched output slices are disjoint; no candidate or input is truncated.
    """
    parts: list[str] = []
    local_start: Optional[int] = None
    copied_until = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in _EMAIL_LOCAL_CHARS:
            if local_start is None:
                previous_is_word = index > 0 and _is_word_character(text[index - 1])
                if previous_is_word != _is_word_character(char):
                    local_start = index
            index += 1
            continue
        if char == "@" and local_start is not None:
            end = _email_match_end(text, index + 1)
            if end is not None:
                parts.append(text[copied_until:local_start])
                parts.append("<EMAIL_ADDRESS>")
                copied_until = end
                index = end
                local_start = None
                # Resume at the match end, not the domain-run end. Word
                # boundaries still use original text, never replacement text.
                continue
        local_start = None
        index += 1
    if not parts:
        return text
    parts.append(text[copied_until:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Remaining PII patterns — order matters (more specific first).
# ---------------------------------------------------------------------------
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

# After email: most specific → least specific to avoid partial overlaps.
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_SSN_RE, "<SSN>"),
    (_CREDIT_CARD_RE, "<CREDIT_CARD>"),
    (_API_KEY_RE, "<API_KEY>"),
    (_IP_RE, "<IP_ADDRESS>"),
    (_PHONE_RE, "<PHONE_NUMBER>"),
]


def redact_pii_in_string(text: str) -> str:
    """Scan *text* for PII patterns and replace each match with its entity token."""
    if not text or ("@" not in text and not _QUICK_CHECK.search(text)):
        return text
    if "@" in text:
        text = _redact_email_addresses(text)
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_pii_in_value(value: Any) -> Any:
    """Apply PII redaction to *value*.

    Handles:
    - ``str`` — scanned directly.
    - ``list`` / ``tuple`` of ``str`` — each element scanned, container preserved.
    - Anything else — returned as-is.
    """
    if isinstance(value, str):
        return redact_pii_in_string(value)
    if isinstance(value, (list, tuple)):
        items = [redact_pii_in_string(item) if isinstance(item, str) else item for item in value]
        if all(a is b for a, b in zip(items, value)):
            return value
        return tuple(items) if isinstance(value, tuple) else items
    return value
