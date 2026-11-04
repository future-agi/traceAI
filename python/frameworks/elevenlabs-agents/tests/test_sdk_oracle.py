"""Second signature oracle: the real ElevenLabs SDK ``construct_event``.

Skipped when the ``elevenlabs`` test extra is not installed. Run against 2.73.0
(latest stable on 2026-10-15) and 2.70.0 (the PRD pin); the webhook code is
byte-identical in both.
"""

import time

import pytest

from conftest import WEBHOOK_SECRET, load_fixture, raw
from traceai_elevenlabs_agents.verify import DEFAULT_TOLERANCE_SECS, SignatureError, sign, verify_signature

BODY = raw(load_fixture("post_call_transcription_otel.json"))

elevenlabs = pytest.importorskip("elevenlabs")


def _sdk_webhooks():
    from elevenlabs.client import ElevenLabs

    return ElevenLabs(api_key="not-used-by-construct-event").webhooks


def test_sdk_construct_event_accepts_what_we_sign():
    header = sign(WEBHOOK_SECRET, BODY)
    event = _sdk_webhooks().construct_event(rawBody=BODY.decode("utf-8"), sig_header=header, secret=WEBHOOK_SECRET)
    assert event["type"] == "post_call_transcription_otel"
    assert verify_signature(BODY, header, WEBHOOK_SECRET)


def test_sdk_and_forwarder_agree_on_rejections():
    from elevenlabs.errors import BadRequestError

    now = int(time.time())
    good = sign(WEBHOOK_SECRET, BODY, now)
    mutated = BODY.replace(b"post_call_transcription_otel", b"post_call_transcription_xxx", 1)
    cases = [
        (BODY, sign("wrong-secret", BODY, now)),
        (mutated, good),
        (BODY, sign(WEBHOOK_SECRET, BODY, now - DEFAULT_TOLERANCE_SECS - 5)),
        (BODY, "t=%d" % now),
    ]
    for body, header in cases:
        with pytest.raises(BadRequestError):
            _sdk_webhooks().construct_event(rawBody=body.decode("utf-8"), sig_header=header, secret=WEBHOOK_SECRET)
        with pytest.raises(SignatureError):
            verify_signature(body, header, WEBHOOK_SECRET)
