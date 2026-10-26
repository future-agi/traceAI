"""Credential scrubbing for anything that can reach a span or a log line.

The Realtime socket carries the OpenAI key in the ``Authorization`` header on a
server, and browsers put it in a WebSocket subprotocol
(``openai-insecure-api-key.<key>``). Neither header is ever read by this package,
but the key can still show up inside text the server or the SDK hands back, such
as an error message or an exception string. Every free-text value goes through
``redact`` before it is written, and the exact key of the client in use is
removed as well as anything shaped like a key.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

REDACTED = "[REDACTED]"

_PATTERNS = (
    # OpenAI secret, project and service-account keys, and Realtime client secrets.
    re.compile(r"\b(?:sk|ek)-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bek_[A-Za-z0-9_\-]{6,}"),
    # The browser subprotocol form carries the key after a fixed prefix.
    re.compile(r"openai-insecure-api-key\.[^\s,;\"']+", re.IGNORECASE),
    # Header and query forms.
    re.compile(r"(?i)\bbearer\s+[^\s,;\"']+"),
    re.compile(r"(?i)\b(authorization|api[_-]?key|x-api-key|x-secret-key)\s*[:=]\s*[^\s,;&\"']+"),
)


def redact(text: object, secrets: Iterable[Optional[str]] = ()) -> str:
    """Return ``text`` as a string with credentials replaced by ``[REDACTED]``."""
    value = text if isinstance(text, str) else str(text)
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in value:
            value = value.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        value = pattern.sub(_replacement, value)
    return value


def _replacement(match: "re.Match[str]") -> str:
    found = match.group(0)
    # Keep an Authorization header's name so the message still reads. Every other
    # form is replaced whole: a left-over "Bearer ", "api_key=" or
    # "openai-insecure-api-key." prefix still looks like a credential to a scanner.
    head, mark, _ = found.partition(":")
    if mark and head.strip().lower() == "authorization":
        return f"{head}{mark} {REDACTED}"
    return REDACTED
