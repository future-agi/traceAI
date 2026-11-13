"""T-PF: preflight tickets before any capture or decode (design section 1)."""

import gc
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fi_instrumentation.attachments import (
    AttachmentConfig, AttachmentManager, AttachmentPolicy, Bytes, CapturePolicy, create_attachment_manager,
)
from fi_instrumentation.attachments.policy import decide
from fi_instrumentation.instrumentation.config import TraceConfig

from conftest import f01, make_manager
from test_policy_runtime import CONTEXT


class Spy:
    """A Bytes-like source whose data access is counted."""

    def __init__(self, data=b"spy-bytes"):
        self.reads, self._data = 0, data

    @property
    def data(self):
        self.reads += 1
        return self._data


def test_disabled_preflight_never_touches_source():
    """T-PF-01."""
    manager = create_attachment_manager(project_name="synthetic")
    spy = Spy()
    ticket = manager.preflight(CONTEXT, mime_type="image/png", direction="output", max_bytes=10)
    assert ticket.reason == "disabled" and not ticket.admitted and spy.reads == 0


def test_bootstrap_pending_reserves_nothing(transport):
    """T-PF-02."""
    gate = threading.Event()
    original = transport.bootstrap
    transport.bootstrap = lambda *a, **kw: (gate.wait(5), original(*a, **kw))[1]
    manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic", transport=transport)
    try:
        ticket = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=100)
        assert ticket.reason == "bootstrap_pending" and manager.owned_bytes == 0
        assert manager.status().open_tickets == 0
    finally:
        gate.set()
        manager.shutdown(1)


@pytest.mark.parametrize("case", f01("privacy-fixtures.json")["cases"], ids=lambda c: c["id"])
def test_privacy_table_fixture(case):
    """T-PF-03: every F01 privacy fixture is reproduced by the vendored table evaluator."""
    reason = decide(case["context"])
    expected = case["expected"]
    if expected["decision"] == "allow":
        assert reason is None
    else:
        assert reason == expected["reason"]


@pytest.mark.parametrize("client,server,admitted", [(False, True, False), (True, False, False), (True, True, True)])
def test_create_recording_needs_both_flags(transport, client, server, admitted):
    """T-PF-04."""
    transport.policy = replace(transport.policy, record_audio=server)
    manager = make_manager(transport, record_audio=client)
    try:
        ticket = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=10,
                                   operation="create_recording")
        assert ticket.admitted == admitted
        if not admitted:
            assert ticket.reason == "recording_disabled"
        ticket.release()
    finally:
        manager.shutdown(1)


def test_import_is_not_recording(transport):
    """T-PF-05."""
    transport.policy = replace(transport.policy, record_audio=False)
    manager = make_manager(transport)
    try:
        ticket = manager.preflight(CONTEXT, mime_type="audio/ogg", direction="mixed", max_bytes=10,
                                   operation="import_existing")
        assert ticket.admitted
        ticket.release()
    finally:
        manager.shutdown(1)


def test_mixed_with_hidden_inputs_denied(manager):
    """T-PF-06."""
    manager.policy = AttachmentPolicy(manager.config, TraceConfig(hide_inputs=True))
    assert manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=1).reason == "policy_denied"


def test_screenshot_keeps_input_ancestry(manager):
    """T-PF-07."""
    manager.policy = AttachmentPolicy(manager.config, TraceConfig(hide_input_images=True))
    ticket = manager.preflight(CONTEXT, mime_type="image/png", direction="output", max_bytes=1,
                               purpose="computer_screenshot")
    assert ticket.reason == "policy_denied"
    allowed = manager.preflight(CONTEXT, mime_type="image/png", direction="output", max_bytes=1,
                                purpose="model_output")
    assert allowed.admitted
    allowed.release()


def test_descriptor_and_byte_exhaustion(transport):
    """T-PF-08."""
    manager = make_manager(transport, max_descriptors=2, max_owned_bytes=1024)
    try:
        first = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=100)
        second = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=100)
        assert first.admitted and second.admitted
        assert manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=1).reason == "queue_full"
        first.release()
        assert manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=1000).reason == "queue_full"
        second.release()
        assert manager.owned_bytes == 0
    finally:
        manager.shutdown(1)


def test_voice_adapters_use_public_preflight_only():
    """T-PF-10 (static half): the core exposes preflight/status(); no adapter in this tree reads _session."""
    root = Path(__file__).resolve().parents[2] / "frameworks"
    offenders = []
    for name in ("pipecat", "livekit"):
        for path in (root / name).rglob("*.py") if (root / name).exists() else []:
            text = path.read_text(errors="ignore")
            if "manager._session" in text or "manager.policy.evaluate" in text:
                offenders.append(str(path))
    assert not offenders, offenders


def test_ticket_single_use(manager, transport):
    """T-PF-11."""
    ticket = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=10)
    assert ticket.attach(Bytes(b"one")).result(2).status == "validating"
    spy = Spy()
    assert ticket.attach(spy).admission == "conflict" and spy.reads == 0


def test_ticket_ttl_is_lazy(manager, monkeypatch):
    """T-PF-12: no timer; expiry returns budget at the next entry point."""
    timers = []
    monkeypatch.setattr(threading, "Timer", lambda *a, **kw: timers.append(a))
    ticket = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=100, ttl=0.05)
    assert manager.owned_bytes == 100
    time.sleep(0.1)
    assert manager.status().owned_bytes == 0
    assert ticket.attach(Bytes(b"x")).admission == "expired"
    assert not timers


def test_ticket_gc_returns_budget(manager):
    ticket = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=100)
    assert manager.owned_bytes == 100
    del ticket
    gc.collect()
    assert manager.owned_bytes == 0


def test_tightening_between_preflight_and_attach(manager, transport):
    """T-PF-13."""
    ticket = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=10)
    manager.update_policy(replace(manager._session.policy, enabled=False))
    spy = Spy()
    handle = ticket.attach(spy)
    assert handle.admission == "policy_denied" and spy.reads == 0
    assert manager.owned_bytes == 0 and not transport.requests


def test_attach_larger_than_ticket(manager, transport):
    """T-PF-14."""
    ticket = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=4)
    assert ticket.attach(Bytes(bytearray(b"12345"))).admission == "too_large"
    assert manager.owned_bytes == 0 and not transport.requests


def test_reservation_shrinks_to_actual(manager, transport):
    transport.release.clear()
    ticket = manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=1000)
    handle = ticket.attach(Bytes(b"abc"))
    assert manager.owned_bytes == 3
    transport.release.set()
    assert handle.result(2).status == "validating"


@pytest.mark.parametrize("mime,modality", [("image/png", "image"), ("application/pdf", "document"),
                                           ("application/octet-stream", "file"), ("video/mp4", "video")])
def test_modalities_follow_f01_map(manager, transport, mime, modality):
    handle = manager.attach(CONTEXT, Bytes(b"payload"), mime_type=mime, direction="output", purpose="model_output")
    assert handle.result(2).status == "validating"
    assert transport.requests[-1]["modality"] == modality


def test_server_modality_gate(transport):
    transport.policy = replace(transport.policy, allowed_modalities=frozenset(("audio",)))
    manager = make_manager(transport)
    try:
        assert manager.preflight(CONTEXT, mime_type="image/png", direction="output", max_bytes=1).reason == "policy_denied"
        assert manager.preflight(CONTEXT, mime_type="image/gif", direction="output", max_bytes=1).reason == "source_unavailable"
    finally:
        manager.shutdown(1)


def test_image_cap(manager):
    assert manager.preflight(CONTEXT, mime_type="image/png", direction="output",
                             max_bytes=manager._limits().max_upload_bytes + 1).reason == "too_large"


def test_pii_redaction_denies_raw_media(manager):
    manager.policy = AttachmentPolicy(manager.config, TraceConfig())
    import os

    os.environ["FI_PII_REDACTION"] = "true"
    try:
        assert manager.preflight(CONTEXT, mime_type="image/png", direction="output",
                                 max_bytes=1).reason == "redaction_unavailable"
    finally:
        del os.environ["FI_PII_REDACTION"]


def test_capture_policy_new_recording_alias(transport):
    transport.policy = replace(transport.policy, record_audio=True)
    manager = make_manager(transport)
    try:
        assert manager.attach(CONTEXT, Bytes(b"x"), mime_type="audio/wav", direction="mixed",
                              capture_policy=CapturePolicy(new_recording=True)).admission == "recording_disabled"
    finally:
        manager.shutdown(1)
