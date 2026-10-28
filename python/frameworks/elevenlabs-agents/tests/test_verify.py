import hashlib
import hmac
import time

import pytest

from conftest import WEBHOOK_SECRET, load_fixture, raw
from traceai_elevenlabs_agents.verify import (
    DEFAULT_TOLERANCE_SECS,
    SignatureError,
    compute_signature,
    sign,
    verify_signature,
)

BODY = raw(load_fixture("post_call_transcription_otel.json"))


def hand_signature(secret: str, timestamp: int, body: bytes) -> str:
    # Primary oracle, written from the docs: HMAC-SHA256 hex of "{timestamp}.{raw_body}".
    return hmac.new(secret.encode(), ("%d." % timestamp).encode() + body, hashlib.sha256).hexdigest()


def test_signature_matches_hand_computed_hmac():
    ts = 1760000000
    assert compute_signature(WEBHOOK_SECRET, ts, BODY) == hand_signature(WEBHOOK_SECRET, ts, BODY)
    assert sign(WEBHOOK_SECRET, BODY, ts) == "t=%d,v0=%s" % (ts, hand_signature(WEBHOOK_SECRET, ts, BODY))


def test_valid_signature_returns_timestamp():
    now = time.time()
    header = sign(WEBHOOK_SECRET, BODY, int(now))
    assert verify_signature(BODY, header, WEBHOOK_SECRET, now=now) == int(now)


@pytest.mark.parametrize(
    "header, reason",
    [
        (None, "missing_signature_header"),
        ("", "missing_signature_header"),
        ("v0=abc", "malformed_signature_header"),
        ("t=1760000000", "malformed_signature_header"),
        ("t=notanumber,v0=abc", "malformed_signature_header"),
    ],
)
def test_missing_or_malformed_header(header, reason):
    with pytest.raises(SignatureError) as info:
        verify_signature(BODY, header, WEBHOOK_SECRET, now=1760000000)
    assert info.value.reason == reason


def test_wrong_secret_and_mutated_byte_fail():
    now = 1760000000
    header = sign(WEBHOOK_SECRET, BODY, now)
    with pytest.raises(SignatureError) as info:
        verify_signature(BODY, header, "another-secret", now=now)
    assert info.value.reason == "signature_mismatch"
    mutated = bytearray(BODY)
    mutated[len(mutated) // 2] ^= 0x01
    with pytest.raises(SignatureError):
        verify_signature(bytes(mutated), header, WEBHOOK_SECRET, now=now)


def test_stale_and_future_timestamps_fail():
    now = 1760000000
    stale = sign(WEBHOOK_SECRET, BODY, now - DEFAULT_TOLERANCE_SECS - 1)
    future = sign(WEBHOOK_SECRET, BODY, now + DEFAULT_TOLERANCE_SECS + 1)
    edge = sign(WEBHOOK_SECRET, BODY, now - DEFAULT_TOLERANCE_SECS)
    for header in (stale, future):
        with pytest.raises(SignatureError) as info:
            verify_signature(BODY, header, WEBHOOK_SECRET, now=now)
        assert info.value.reason == "timestamp_outside_tolerance"
    assert verify_signature(BODY, edge, WEBHOOK_SECRET, now=now)
    # A configurable window.
    with pytest.raises(SignatureError):
        verify_signature(BODY, sign(WEBHOOK_SECRET, BODY, now - 120), WEBHOOK_SECRET, tolerance_secs=60, now=now)


def test_header_with_spaces_and_several_v0_entries():
    now = 1760000000
    good = compute_signature(WEBHOOK_SECRET, now, BODY)
    header = "t=%d, v0=%s, v0=%s" % (now, "0" * 64, good)
    assert verify_signature(BODY, header, WEBHOOK_SECRET, now=now) == now


def test_no_secret_configured_fails_closed():
    with pytest.raises(SignatureError) as info:
        verify_signature(BODY, sign("x", BODY), "")
    assert info.value.reason == "webhook_secret_not_configured"
