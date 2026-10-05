"""Text cleaning applied to every string the callback writes to a span.

Order for every value: remove secrets, then (when a TraceConfig hide flag is
on) remove text the run already saw as an input or output, then PII when
``pii_redaction`` is on, then cut to a UTF-8 byte cap. Secrets go first so a
cap can never leave the start of a key behind; PII goes before the cap so a
cut cannot leave part of an email address.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, List, Optional, Sequence, Tuple

from fi_instrumentation import REDACTED_VALUE
from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string

REDACTED = "[redacted]"

MAX_VALUE_BYTES = 4096
MAX_NAME_BYTES = 256
MAX_ERROR_BYTES = 1024
MAX_STACKTRACE_BYTES = 16 * 1024

# A secret shorter than this is not searched for: replacing a 3-character
# value everywhere would shred unrelated text.
MIN_SECRET_CHARS = 8
# Known inputs/outputs shorter than this are only removed in their quoted
# forms ('ab', "ab"); bare short strings would match inside unrelated words.
MIN_SCRUB_CHARS = 3
# Bound on the text one agent run remembers for hide_inputs/hide_outputs.
MAX_KNOWN_TEXTS = 2048
MAX_KNOWN_CHARS = 4 * 1024 * 1024

_SECRET_ENV_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)
_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNED = re.compile(
    r"(?i)\b((?:x[-_])?api[-_]?key|authorization|secret(?:[-_]?key)?|access[-_]?token"
    r"|refresh[-_]?token|token|password|passwd)"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"',;}\]]{4,})"
)
_KEY_SHAPE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")


def cap(value: str, limit: int) -> str:
    """Return the longest whole-character prefix of at most ``limit`` UTF-8 bytes."""
    return value.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def cap_tail(value: str, limit: int) -> str:
    """Return the longest whole-character suffix of at most ``limit`` UTF-8 bytes.

    Used for stack traces, whose last lines (the raising frame and the
    exception line) matter most.
    """
    encoded = value.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return value
    return encoded[len(encoded) - limit :].decode("utf-8", "ignore")


class Secrets:
    """Secret values the callback removes from every text it writes.

    Sources: the values passed as ``redact=`` and, read on every call, the
    values of environment variables whose name contains KEY, SECRET, TOKEN,
    PASSWORD, PASSWD, CREDENTIAL or AUTH (LLM provider keys such as
    OPENAI_API_KEY, and FI_API_KEY / FI_SECRET_KEY). Values shorter than
    ``MIN_SECRET_CHARS`` are ignored. Bearer/Basic credentials, ``sk-``
    style keys and ``api_key=...`` / ``Authorization: ...`` assignments are
    also replaced by pattern, for secrets the callback was never told about.
    """

    def __init__(self, explicit: Iterable[str] = ()) -> None:
        self._explicit: Tuple[str, ...] = tuple(
            value for value in dict.fromkeys(explicit) if len(value) >= MIN_SECRET_CHARS
        )

    def values(self) -> List[str]:
        found = list(self._explicit)
        try:
            for name, value in list(os.environ.items()):
                if (
                    value
                    and len(value) >= MIN_SECRET_CHARS
                    and _SECRET_ENV_NAME.search(name)
                    and value not in found
                ):
                    found.append(value)
        except Exception:  # an unreadable environment must not break redaction
            pass
        return sorted(found, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for value in self.values():
            if value in text:
                text = text.replace(value, REDACTED)
        text = _BEARER.sub(lambda match: match.group(1) + match.group(2) + REDACTED, text)
        text = _ASSIGNED.sub(lambda match: match.group(1) + match.group(2) + REDACTED, text)
        return _KEY_SHAPE.sub(REDACTED, text)


class KnownTexts:
    """Inputs (or outputs) one agent run has seen, for ``hide_inputs``/``hide_outputs``.

    Every text is stored raw, with secrets already removed (so it still
    matches after the secret pass), and in its Python-repr and JSON-quoted
    forms (so an echo inside a repr'd dict or a JSON body matches too).
    Past ``MAX_KNOWN_TEXTS`` entries or ``MAX_KNOWN_CHARS`` characters the
    set is marked overflowed and ``scrub`` fails closed by returning None.
    """

    def __init__(self) -> None:
        self._items: set = set()
        self._chars = 0
        self._sorted: Optional[Sequence[str]] = None
        self.overflowed = False

    def add(self, text: str, secrets: Secrets) -> None:
        if not isinstance(text, str) or not text or self.overflowed:
            return
        forms = {repr(text), json.dumps(text, ensure_ascii=False), json.dumps(text)}
        if len(text) >= MIN_SCRUB_CHARS:
            forms.add(text)
            redacted = secrets.redact(text)
            if len(redacted) >= MIN_SCRUB_CHARS:
                forms.add(redacted)
        for form in forms:
            if form in self._items:
                continue
            self._items.add(form)
            self._chars += len(form)
        self._sorted = None
        if len(self._items) > MAX_KNOWN_TEXTS or self._chars > MAX_KNOWN_CHARS:
            self.overflowed = True
            self._items.clear()

    def scrub(self, text: str) -> Optional[str]:
        if self.overflowed:
            return None
        if self._sorted is None:
            self._sorted = sorted(self._items, key=len, reverse=True)
        for item in self._sorted:
            if item in text:
                text = text.replace(item, REDACTED_VALUE)
        return text


def clean(
    text: str,
    secrets: Secrets,
    limit: int,
    pii: bool = False,
    scrub: Sequence[KnownTexts] = (),
    tail: bool = False,
) -> Optional[str]:
    """Secrets, then known inputs/outputs, then PII, then the byte cap.

    ``tail`` keeps the end of the text instead of the start. Returns None
    when a hide flag's text set overflowed: the caller must then record
    nothing rather than an unscrubbed value.
    """
    text = secrets.redact(text)
    for known in scrub:
        scrubbed = known.scrub(text)
        if scrubbed is None:
            return None
        text = scrubbed
    if pii:
        text = redact_pii_in_string(text)
    return cap_tail(text, limit) if tail else cap(text, limit)
