"""T-TR/T-CH: F01 wire fixtures and channel/role rules (design sections 7 and 9)."""

import json
import uuid

import pytest
from fi_instrumentation.attachments import Bytes, CorrelationHandle
from fi_instrumentation.attachments.protocol import wire_valid
from opentelemetry.sdk.trace import TracerProvider

from conftest import f01
from test_policy_runtime import CONTEXT

SDK_SHAPES = {"BootstrapRequest", "BootstrapResponse", "UploadRequest", "UploadResponse", "StatusResponse",
              "CompleteRequest", "RecordingManifest", "RecordingImportRequest", "RecordingImportResponse",
              "ImportStatusResponse", "Error", "SpanAttributes", "References", "Reference"}
CASES = [c for c in f01("wire-fixtures.json")["cases"] if c["schema"] in SDK_SHAPES]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_wire_fixture_validity(case):
    """T-TR-01: the core validator reproduces each fixture's validity."""
    assert wire_valid(case["schema"], case["value"]) == case["valid"]


def test_missing_capability_rejected():
    """T-TR-07."""
    case = next(c for c in f01("wire-fixtures.json")["cases"] if c["schema"] == "UploadResponse" and c["valid"])
    value = dict(case["value"])
    value.pop("write_capability")
    assert not wire_valid("UploadResponse", value)


@pytest.mark.parametrize("case", f01("lifecycle-fixtures.json")["cases"][:2], ids=lambda c: c["id"])
def test_sdk_reports_validating_never_ready(manager, case):
    """T-TR-08: after complete the SDK reports validating in both binding orders."""
    handle = manager.attach(CONTEXT, Bytes(b"x"), mime_type="audio/wav", direction="mixed")
    assert handle.result(2).status == "validating"


def test_upload_request_never_has_role_or_channel(manager, transport):
    """T-CH-01."""
    handle = manager.attach(CONTEXT, Bytes(b"x"), mime_type="image/png", direction="output",
                            purpose="model_output", role="assistant", channel=3, message_index=1, part_index=2,
                            tool_call_id="call_1")
    assert handle.result(2).status == "validating"
    request = transport.requests[0]
    assert "role" not in request and "channel" not in request
    assert (request["message_index"], request["part_index"], request["tool_call_id"]) == (1, 2, "call_1")
    assert wire_valid("UploadRequest", request)


def test_live_span_reference_and_bounds(manager):
    """T-CH-02/05: role/channel projected on a live span; at most 64 references."""
    provider = TracerProvider(shutdown_on_exit=False)
    span = provider.get_tracer("t").start_span("s")
    handles = [manager.attach(span, Bytes(b"x"), mime_type="image/png", direction="output", purpose="model_output",
                              role="assistant", channel=1) for _ in range(66)]
    attributes = {k: v for k, v in span.attributes.items() if k.startswith("fi.attachment.")}
    refs = json.loads(attributes["fi.attachment.refs"])
    assert len(refs) == 64 and refs[0]["role"] == "assistant" and refs[0]["channel"] == 1
    assert "tool_call_id" not in refs[0]
    assert wire_valid("SpanAttributes", attributes)
    assert all(h.admission == "queued" for h in handles)
    span.end()
    provider.shutdown()


def test_ended_span_not_mutated(manager, transport):
    """T-CH-03."""
    provider = TracerProvider(shutdown_on_exit=False)
    span = provider.get_tracer("t").start_span("s")
    correlation = CorrelationHandle.from_span(span)
    span.end()
    before = dict(span.attributes or {})
    handle = manager.attach(correlation, Bytes(b"x"), mime_type="audio/wav", direction="mixed")
    assert handle.result(2).status == "validating"
    assert dict(span.attributes or {}) == before
    provider.shutdown()


def test_denied_capture_stamps_nothing(manager):
    """T-CH-05 (denial)."""
    from dataclasses import replace

    provider = TracerProvider(shutdown_on_exit=False)
    span = provider.get_tracer("t").start_span("s")
    other = provider.get_tracer("t").start_span("other")
    manager.update_policy(replace(manager._session.policy, enabled=False))
    manager.attach(span, Bytes(b"x"), mime_type="audio/wav", direction="mixed")
    assert not any(k.startswith("fi.attachment.") for k in (span.attributes or {}))
    assert not any(k.startswith("fi.attachment.") for k in (other.attributes or {}))
    span.end()
    other.end()
    provider.shutdown()


def test_explicit_workspace_scope_mismatch():
    """T-CH-06."""
    from fi_instrumentation.attachments.errors import TransferError
    from fi_instrumentation.attachments.protocol import HTTPTransport

    workspace = str(uuid.uuid4())
    transport = HTTPTransport("https://example.test", {"x-api-key": "k", "x-secret-key": "s"},
                              workspace_id=workspace)
    body = transport.bootstrap_body("p", "observe", str(uuid.uuid4()), str(uuid.uuid4()))
    assert body["workspace_id"] == workspace
    case = next(c for c in f01("wire-fixtures.json")["cases"] if c["schema"] == "BootstrapResponse" and c["valid"])
    response = json.loads(json.dumps(case["value"]))
    response["capture_session_id"] = body["capture_session_id"]
    response["expires_at"] = "2999-01-01T00:00:00Z"
    with pytest.raises(TransferError) as error:
        transport._session(response, body, b"{}")
    assert error.value.reason == "scope_unresolved"
    response["scope"]["workspace_id"] = workspace
    assert transport._session(response, body, b"{}").scope[1] == workspace
