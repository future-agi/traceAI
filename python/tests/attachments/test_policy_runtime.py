import asyncio
import json
import os
import signal
import threading
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanContext, TraceFlags

from fi_instrumentation.attachments import (
    AttachmentConfig, AttachmentManager, AttachmentPolicy, Bytes, CapturePolicy,
    CorrelationHandle, LocalFile, ProviderFile, RemoteUrl, ServerPolicy,
)
from fi_instrumentation.attachments.protocol import TransferError
from fi_instrumentation.instrumentation.config import TraceConfig, suppress_tracing

CONTEXT = CorrelationHandle("1234567890abcdef1234567890abcdef", "1234567890abcdef", True)


def attach(manager, data=b"synthetic-audio", **kwargs):
    return manager.attach(CONTEXT, Bytes(data), mime_type="audio/wav", direction="mixed", **kwargs)


def test_default_no_workers_no_network(monkeypatch):
    manager = AttachmentManager(project_name="synthetic")
    assert attach(manager).admission == "disabled"
    assert manager.owned_bytes == 0
    assert not manager._threads and manager._transport is None and manager._p is None
    assert manager.shutdown(0).unfinished == 0


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages", "hide_input_text", "hide_output_text", "pii_redaction"])
def test_denial_before_even_payload_access(manager, transport, flag):
    # A hostile byte wrapper proves denial occurs before source.data is read,
    # not merely before sending a mock HTTP request.
    class Forbidden:
        @property
        def data(self):
            raise AssertionError("source was read")
    manager.policy = AttachmentPolicy(manager.config, TraceConfig(**{flag: True}))
    result = manager.attach(CONTEXT, Forbidden(), mime_type="audio/wav", direction="mixed")
    assert result.admission in ("policy_denied", "redaction_unavailable")
    assert not transport.requests and not transport.payloads
    assert manager.owned_bytes == 0
    assert manager.status().queued == 0
    assert not hasattr(result, "source")


@pytest.mark.parametrize("direction", ["input", "output", "mixed"])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages", "hide_input_text", "hide_output_text"])
def test_directional_lattice(manager, transport, direction, flag):
    manager.policy = AttachmentPolicy(manager.config, TraceConfig(**{flag: True}))
    denied = direction == "mixed" or direction in flag
    result = manager.attach(CONTEXT, Bytes(b"isolated"), mime_type="audio/wav", direction=direction)
    assert (result.admission == "policy_denied") == denied
    if not denied:
        assert result.result(2).status == "validating"
        assert transport.payloads == [b"isolated"]
    else:
        assert not transport.requests


@pytest.mark.parametrize("field", ["capture_content", "record_inputs", "record_outputs", "requested"])
def test_integration_denials(manager, transport, field):
    assert attach(manager, capture_policy=CapturePolicy(**{field: False})).admission == "policy_denied"
    assert not transport.requests


@pytest.mark.parametrize("field", ["muted", "temporary_chat"])
def test_session_denials(manager, transport, field):
    assert attach(manager, capture_policy=CapturePolicy(**{field: True})).admission == "policy_denied"
    assert not transport.requests


def test_error_and_semantic_ancestry(manager, transport):
    manager.policy = AttachmentPolicy(manager.config, TraceConfig(hide_error_details=True, hide_inputs=True))
    assert attach(manager, capture_policy=CapturePolicy(error_detail=True)).admission == "policy_denied"
    assert manager.attach(CONTEXT, Bytes(b"x"), mime_type="audio/wav", direction="output",
                          capture_policy=CapturePolicy(contains_input=True)).admission == "policy_denied"
    assert not transport.requests


@pytest.mark.parametrize("client", [False, True])
@pytest.mark.parametrize("server", [False, True])
@pytest.mark.parametrize("mandatory", [False, True])
def test_pii_exception_intersection(manager, client, server, mandatory):
    manager.policy = AttachmentPolicy(AttachmentConfig(enabled=True, allow_unredacted_media_with_text_pii=client), TraceConfig(pii_redaction=True))
    manager.update_policy(replace(manager._session.policy, allow_unredacted_media_with_text_pii=server, require_media_redaction=mandatory))
    result = attach(manager)
    assert (result.admission == "queued") == (client and server and not mandatory)


def test_metadata_removal_requirement(manager, transport):
    manager.update_policy(replace(manager._session.policy, require_metadata_removal=True))
    assert attach(manager).admission == "redaction_unavailable"
    assert not transport.requests


def test_recording_is_not_import_permission(manager, transport):
    assert attach(manager, capture_policy=CapturePolicy(new_recording=True)).admission in ("policy_denied", "recording_disabled")
    assert attach(manager).result(2).status == "validating"
    assert transport.payloads == [b"synthetic-audio"]


@pytest.mark.parametrize("value", ["false", "0", "invalid"])
def test_env_disable_wins_explicit_enable(manager, transport, monkeypatch, value):
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", value)
    assert attach(manager).admission == "disabled"
    assert not transport.requests


def test_live_hide_env_rechecked_before_transfer(manager, transport, monkeypatch):
    transport.release.clear()
    handle = attach(manager)
    assert transport.entered.wait(2)
    monkeypatch.setenv("FI_HIDE_INPUT_TEXT", "true")
    transport.release.set()
    assert handle.result(2).status == "policy_denied"
    assert not transport.payloads
    assert manager.owned_bytes == 0


def test_mutable_buffer_is_owned_only_after_admission(manager, transport):
    transport.release.clear()
    data = bytearray(b"before")
    handle = attach(manager, data)
    assert transport.entered.wait(2)
    data[:] = b"after!"
    transport.release.set()
    assert handle.result(2).status == "validating"
    assert transport.payloads == [b"before"]


def test_unknown_bootstrap_drops_without_retaining(transport):
    gate = threading.Event()
    original = transport.bootstrap
    transport.bootstrap = lambda *args, **kw: (gate.wait(2), original(*args, **kw))[1]
    manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic", transport=transport)
    assert attach(manager).admission == "bootstrap_pending"
    assert manager.owned_bytes == 0 and manager.status().queued == 0
    gate.set()
    assert manager.ready(2)
    assert not transport.requests
    manager.shutdown(0)


@pytest.mark.parametrize("source", [RemoteUrl("https://127.0.0.1/private?secret=canary"), RemoteUrl("https://169.254.169.254/"), RemoteUrl("https://[::1]/"), LocalFile("/never-read"), ProviderFile("livekit", "opaque-id"), "not-a-source"])
def test_unqualified_sources_never_retrieve(manager, transport, monkeypatch, source):
    manager.update_policy(replace(manager._session.policy, allowed_sources=frozenset((type(source).__name__,))))
    monkeypatch.setattr("socket.getaddrinfo", Mock(side_effect=AssertionError("DNS")))
    monkeypatch.setattr("builtins.open", Mock(side_effect=AssertionError("file read")))
    result = manager.attach(CONTEXT, source, mime_type="audio/wav", direction="mixed")
    assert result.admission == "source_unavailable"
    assert not transport.requests and manager.status().queued == 0


def test_trace_identity_and_ended_span(manager, transport):
    provider = TracerProvider(shutdown_on_exit=False)
    span = provider.get_tracer("test").start_span("synthetic")
    correlation = CorrelationHandle.from_span(span)
    first = manager.attach(span, Bytes(b"one"), mime_type="audio/wav", direction="mixed", channel=1)
    refs = json.loads(span.attributes["fi.attachment.refs"])
    assert refs[0]["occurrence_id"] == first.occurrence_id
    span.end()
    attributes = dict(span.attributes)
    late = manager.attach(correlation, Bytes(b"late"), mime_type="audio/wav", direction="mixed")
    assert late.result(2).status == "validating"
    assert dict(span.attributes) == attributes
    assert manager.attach(span, Bytes(b"no"), mime_type="audio/wav", direction="mixed").admission == "unsampled"
    assert all(req["association"]["parent"]["trace_id"] == correlation.trace_uuid for req in transport.requests)
    provider.shutdown()


@pytest.mark.parametrize("value", ["0" * 32, "ABCDEF1234567890abcdef1234567890", "123", "g" * 32])
def test_invalid_id_rejected(value):
    with pytest.raises(ValueError):
        CorrelationHandle(value, "1234567890abcdef", True)


def test_uuid_conversion_preserves_all_bits():
    context = CorrelationHandle("ffffffffffffffffffffffffffffffff", "ffffffffffffffff", True)
    assert context.trace_uuid == "ffffffff-ffff-ffff-ffff-ffffffffffff"
    assert CorrelationHandle(context.trace_uuid, context.span_id, True) == context


def test_queue_and_memory_include_active(manager, transport):
    manager.config = replace(manager.config, max_descriptors=1, max_owned_bytes=8)
    transport.release.clear()
    first = attach(manager, b"12345678")
    assert transport.entered.wait(2)
    assert manager.owned_bytes == 8
    assert attach(manager, b"x").admission == "queue_full"
    transport.release.set()
    assert first.result(2).status == "validating"
    assert manager.owned_bytes == 0


def test_oversize_rejected_before_copy(transport):
    """T-PF-15: a mutable buffer above sync_copy_bytes is refused; immutable bytes are not copied."""
    from conftest import make_manager

    manager = make_manager(transport, sync_copy_bytes=1024)
    try:
        assert attach(manager, bytearray(1025)).admission == "too_large"
        assert attach(manager, b"x" * 1025).result(2).status == "validating"
        assert attach(manager, b"x" * (manager._limits().max_upload_bytes + 1)).admission == "too_large"
        assert len(transport.requests) == 1
    finally:
        manager.shutdown(1)


def test_policy_tightening_discards_queue_and_does_not_resurrect(manager, transport):
    transport.release.clear()
    first = attach(manager)
    assert transport.entered.wait(2)
    handles = [attach(manager) for _ in range(5)]
    old = manager._session.policy
    manager.update_policy(replace(old, enabled=False))
    manager.update_policy(old)
    transport.release.set()
    assert first.result(2).status == "policy_denied"
    assert all(handle.result(2).status == "policy_denied" for handle in handles)
    assert not transport.payloads and manager.owned_bytes == 0


def test_retries_keep_upload_identity_and_are_bounded(manager, transport):
    transport.failures = [TransferError("incomplete", True), TransferError("throttled", True)]
    assert attach(manager).result(2).status == "validating"
    assert transport.calls == 3 and len(transport.requests) == 1


@pytest.mark.parametrize("reason", ["policy_denied", "credential_scope_unsupported", "expired", "validation_failed"])
def test_security_failures_never_retry(manager, transport, reason):
    transport.failures = [TransferError(reason)]
    assert attach(manager).result(2).status == reason
    assert transport.calls == 1


def test_shutdown_deadline_cancel_and_signal_handlers(manager, transport):
    transport.release.clear()
    handler = signal.getsignal(signal.SIGTERM)
    handle = attach(manager)
    assert transport.entered.wait(2)
    start = time.monotonic()
    result = manager.shutdown(0.02)
    assert time.monotonic() - start < 0.27 and result.unfinished == 1
    assert signal.getsignal(signal.SIGTERM) == handler
    transport.release.set()
    assert handle.result(2).status == "incomplete"
    assert manager.shutdown(0) == result


def test_async_completion_and_flush(manager):
    async def run():
        handle = attach(manager)
        assert (await handle.wait(2)).status == "validating"
        assert (await manager.flush_async(2)).unfinished == 0
    asyncio.run(run())


def test_uploader_exception_body_not_retained(manager, transport, caplog):
    transport.failures = [RuntimeError("CANARY_SIGNED_PATH_SECRET")]
    handle = attach(manager)
    assert handle.result(2).status == "incomplete"
    assert "CANARY" not in repr(vars(handle)) + caplog.text
    assert manager.owned_bytes == 0


def test_suppressed_capture_has_no_recursion(manager, transport):
    with suppress_tracing():
        assert attach(manager).admission == "policy_denied"
    assert not transport.requests
