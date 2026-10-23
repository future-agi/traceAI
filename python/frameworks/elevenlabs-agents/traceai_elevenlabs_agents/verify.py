"""Verify the ``ElevenLabs-Signature`` header with the standard library only.

ElevenLabs signs every workspace webhook delivery with
``ElevenLabs-Signature: t={unix},v0={hmac}``, where ``hmac`` is the hex
HMAC-SHA256 of ``{timestamp}.{raw_body}`` keyed with the webhook secret.

Sources (re-read 2026-10-15):

* https://elevenlabs.io/docs/eleven-agents/customization/opentelemetry-traces
  (Delivery table: header format and signed string)
* https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks
  (verify with the SDK ``construct_event``)
* ``elevenlabs`` Python SDK ``webhooks_custom.WebhooksClient.construct_event``
  (2.70.0 and 2.73.0 are byte-identical): HMAC-SHA256, ``v0=`` prefix,
  deliveries older than 30 minutes are rejected.

The SDK is not a runtime dependency. Tests use it as a second oracle.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional, Union

#: Default replay window, the one ``construct_event`` enforces (30 minutes).
DEFAULT_TOLERANCE_SECS = 30 * 60

SIGNATURE_HEADER = "ElevenLabs-Signature"


class SignatureError(Exception):
    """The delivery is not authentic. The ``reason`` is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse_header(header: str) -> "tuple[str, list[str]]":
    timestamp = ""
    signatures = []
    for part in header.split(","):
        part = part.strip()
        if part.startswith("t="):
            timestamp = part[2:].strip()
        elif part.startswith("v0="):
            signatures.append(part[3:].strip())
    return timestamp, signatures


def compute_signature(secret: str, timestamp: Union[int, str], raw_body: Union[bytes, str]) -> str:
    """Return the ``v0`` hex digest ElevenLabs would send for this body."""
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    message = str(timestamp).encode("utf-8") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign(secret: str, raw_body: Union[bytes, str], timestamp: Optional[int] = None) -> str:
    """Build an ``ElevenLabs-Signature`` header value. Used by tests and the local stand-in sender."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    return "t={0},v0={1}".format(ts, compute_signature(secret, ts, raw_body))


def verify_signature(
    raw_body: bytes,
    header: Optional[str],
    secret: str,
    *,
    tolerance_secs: int = DEFAULT_TOLERANCE_SECS,
    now: Optional[float] = None,
) -> int:
    """Check one delivery. Returns the signed timestamp, raises :class:`SignatureError`.

    The body is only hashed here, never parsed: a forged delivery is never decoded.
    A timestamp older than ``tolerance_secs`` fails, as in the SDK. A timestamp
    more than ``tolerance_secs`` in the future also fails (the SDK does not check
    that side; a real delivery is never that far ahead).
    """
    if not secret:
        raise SignatureError("webhook_secret_not_configured")
    if not header:
        raise SignatureError("missing_signature_header")
    timestamp, signatures = _parse_header(header)
    if not timestamp or not signatures:
        raise SignatureError("malformed_signature_header")
    try:
        ts = int(timestamp)
    except ValueError:
        raise SignatureError("malformed_signature_header") from None
    current = time.time() if now is None else now
    if ts < current - tolerance_secs or ts > current + tolerance_secs:
        raise SignatureError("timestamp_outside_tolerance")
    expected = compute_signature(secret, timestamp, raw_body)
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("signature_mismatch")
    return ts
