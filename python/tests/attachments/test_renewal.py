"""T-RN: session and upload-capability renewal (design section 3)."""

import json
import logging
import time
import uuid
from dataclasses import replace

from fi_instrumentation.attachments import Bytes, PcmFormat, RecordingOptions, TrackSpec
from fi_instrumentation.attachments.errors import TransferError
from fi_instrumentation.attachments.protocol import Upload

from conftest import TransportDouble, make_manager
from test_policy_runtime import CONTEXT


def wait_for(predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_session_renewal_keeps_ids(transport):
    """T-RN-01."""
    transport.session_ttl = 30           # inside the 60 s renewal window
    manager = make_manager(transport)
    try:
        sid, nonce = manager.capture_session_id, manager.correlation_nonce
        transport.session_ttl = 600      # the renewed session gets a fresh lifetime
        manager.status()                 # lazy activity schedules renewal
        assert wait_for(lambda: transport.session_renewals)
        assert transport.session_renewals[0] == 1
        assert wait_for(lambda: manager._session.expires_at > time.time() + 100)
        assert (manager.capture_session_id, manager.correlation_nonce) == (sid, nonce)
        body = json.loads(manager._session.request)
        assert body["capture_session_id"] == sid and body["correlation_nonce"] == nonce
    finally:
        manager.shutdown(1)


def test_renewal_scope_change_invalidates(transport):
    """T-RN-02."""
    transport.session_ttl = 30
    transport.renew_scope = "00000000-0000-4000-8000-0000000000ff"
    manager = make_manager(transport)
    try:
        manager.status()
        assert wait_for(lambda: manager.status().state == "scope_unresolved")
        assert manager.attach(CONTEXT, Bytes(b"x"), mime_type="audio/wav", direction="mixed").admission == "scope_unresolved"
    finally:
        manager.shutdown(1)


def test_renewal_tighter_policy_drops_queued(transport):
    """T-RN-03."""
    transport.session_ttl = 30
    transport.release.clear()
    manager = make_manager(transport)
    try:
        first = manager.attach(CONTEXT, Bytes(b"one"), mime_type="audio/wav", direction="mixed")
        assert transport.entered.wait(2)
        transport.renew_policy = replace(transport.policy, enabled=False, version="v2")
        manager.status()
        assert wait_for(lambda: manager.status() and manager._session.policy.version == "v2")
        transport.release.set()
        assert first.result(3).status == "policy_denied"
        assert manager.owned_bytes == 0
    finally:
        transport.release.set()
        manager.shutdown(1)


def test_renewal_failure_expires_without_new_session(transport):
    """T-RN-04."""
    transport.session_ttl = 6            # expiring within the 5 s skew margin after ~1 s
    transport.renew_result = "fail"
    manager = make_manager(transport)
    try:
        sid = manager.capture_session_id
        assert wait_for(lambda: manager.status().state == "expired", 5)
        assert manager.attach(CONTEXT, Bytes(b"x"), mime_type="audio/wav", direction="mixed").admission == "expired"
        assert manager.capture_session_id == sid and transport.bootstraps == 1
    finally:
        manager.shutdown(1)


def test_capability_renewed_before_use(manager, transport):
    """T-RN-05: an expiring capability is replayed with the byte-identical init body."""
    original = transport.initialize

    def short(session, body, **kw):
        upload = original(session, body, **kw)
        return Upload(upload.upload_id, time.time() + 1, upload.capability, upload.reserved_bytes)
    transport.initialize = short
    handle = manager.attach(CONTEXT, Bytes(b"payload"), mime_type="audio/wav", direction="mixed")
    assert handle.result(3).status == "validating"
    assert transport.renewals and transport.renewals[0] == json.dumps(transport.requests[0], separators=(",", ":")).encode()
    caps = {cap for kind, cap in transport.capability_used}
    assert len(caps) == 1                # only the fresh capability was used
    key = [k for k in transport.keys if k.startswith("up-")][0]
    assert key == "up-" + uuid.UUID(handle.occurrence_id).hex


def test_stale_capability_on_replay_expires(manager, transport):
    """T-RN-06."""
    transport.renew_result = "same"
    transport.failures = [TransferError("credential_scope_unsupported", http_status=401)]
    handle = manager.attach(CONTEXT, Bytes(b"payload"), mime_type="audio/wav", direction="mixed")
    assert handle.result(3).status == "expired"
    assert transport.aborts and len(transport.requests) == 1


def test_401_renews_once(manager, transport):
    """T-RN-07."""
    transport.failures = [TransferError("credential_scope_unsupported", http_status=401)]
    assert manager.attach(CONTEXT, Bytes(b"a"), mime_type="audio/wav", direction="mixed").result(3).status == "validating"
    transport.failures = [TransferError("credential_scope_unsupported", http_status=401)] * 2
    assert manager.attach(CONTEXT, Bytes(b"b"), mime_type="audio/wav",
                          direction="mixed").result(3).status == "credential_scope_unsupported"


def test_capability_never_leaks(manager, transport, caplog):
    """T-RN-08: canary scan of logs, reprs, status and span attributes."""
    caplog.set_level(logging.DEBUG)
    handle = manager.attach(CONTEXT, Bytes(b"payload"), mime_type="audio/wav", direction="mixed")
    assert handle.result(3).status == "validating"
    writer = manager.start_recording(CONTEXT, RecordingOptions(PcmFormat(24000), "mixed",
                                                               (TrackSpec((0,), "mixed", "unknown"),),
                                                               "observe_consumed"))
    writer.try_write(b"\x00\x00" * 24000)
    writer.finish(5)
    surfaces = caplog.text + repr(handle) + repr(manager.status()) + repr(writer) + repr(writer.status())
    surfaces += repr(manager._session)
    for _, cap in transport.capability_used:
        assert cap not in surfaces
    for upload in transport.uploads.values():
        assert upload.capability not in repr(upload)
