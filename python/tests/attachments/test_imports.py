"""T-IM: completed-recording import seam (design section 5)."""

import asyncio
import json
import uuid

import pytest
from fi_instrumentation.attachments import (
    CompletedRecordingRequest, EnvelopeSource, ProviderFileSource, ResolvedCompletedRecording, UnavailableResolver,
)
from fi_instrumentation.attachments.errors import TransferError
from fi_instrumentation.attachments.protocol import wire_valid

from conftest import f01
from test_policy_runtime import CONTEXT

CONNECTION = "00000000-0000-4000-8000-00000000c001"
OUTPUT = "00000000-0000-4000-8000-00000000c002"
REQUEST = CompletedRecordingRequest("livekit_egress", "EG_synthetic_1")


class Resolver:
    def __init__(self, result=None, error=None, delay=0):
        self.calls, self.result, self.error, self.delay = 0, result, error, delay

    async def resolve(self, request, *, deadline):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


def hour(direction="mixed", source=None, size=172800044, duration=3600000):
    return ResolvedCompletedRecording(source or ProviderFileSource(CONNECTION, OUTPUT), size, duration,
                                      "audio/wav", direction)


def run(manager, resolver, request=REQUEST, timeout=5):
    return asyncio.run(manager.import_completed_recording(CONTEXT, request, resolver=resolver, timeout=timeout))


def test_provider_file_import(manager, transport):
    """T-IM-01/09: request validates; key im-{rid}; original credentials, never a capability."""
    handle = run(manager, Resolver(hour()))
    assert handle.reason == "queued"
    assert handle.result(3).status == "importing" and handle.job_id
    body, key = transport.imports[0]
    assert wire_valid("RecordingImportRequest", body)
    assert key == "im-" + uuid.UUID(handle.recording_id).hex
    assert body["recording"]["declared_bytes"] == 172800044 and body["source"]["kind"] == "ProviderFile"
    assert not [c for k, c in transport.capability_used if k == "import"]


def test_f01_completed_import_fixture_shape(manager, transport):
    cases = {c["id"]: c for c in f01("recording-import-fixtures.json")["cases"]}
    assert "one-hour-completed-provider-admitted" in cases
    run(manager, Resolver(hour())).result(3)
    body = transport.imports[0][0]
    assert set(body) == {"mode", "recording", "source"}


def test_denied_preflight_never_resolves(manager):
    """T-IM-02."""
    from dataclasses import replace

    manager.update_policy(replace(manager._session.policy, enabled=False))
    resolver = Resolver(hour())
    assert run(manager, resolver).reason == "policy_denied" and resolver.calls == 0


@pytest.mark.parametrize("resolver", [Resolver(error=RuntimeError("CANARY provider secret")), Resolver(hour(), delay=5)])
def test_resolver_failure_is_source_unavailable(manager, transport, resolver):
    """T-IM-03."""
    handle = run(manager, resolver, timeout=0.5)
    assert handle.reason == "source_unavailable" and "CANARY" not in repr(handle)
    assert not transport.imports


@pytest.mark.parametrize("size,duration", [(1073741825, 1000), (1000, 7200001)])
def test_out_of_range_is_too_large(manager, transport, size, duration):
    """T-IM-04."""
    assert run(manager, Resolver(hour(size=size, duration=duration))).reason == "too_large"
    assert not transport.imports


def test_mixed_cannot_be_narrowed(manager, transport):
    """T-IM-05."""
    assert run(manager, Resolver(hour(direction="output"))).reason == "policy_denied"
    widened = run(manager, Resolver(hour(direction="mixed")),
                  CompletedRecordingRequest("livekit_egress", "EG_2", direction="output"))
    assert widened.result(3).status == "importing"
    assert transport.imports[-1][0]["recording"]["direction"] == "mixed"


def test_envelope_source(manager, transport):
    """T-IM-06."""
    envelope = "00000000-0000-4000-8000-00000000e001"
    handle = run(manager, Resolver(hour(source=EnvelopeSource(envelope))))
    assert handle.result(3).status == "importing"
    body = transport.imports[0][0]
    assert body["source"] == {"kind": "RemoteUrlEnvelope", "envelope_id": envelope}
    assert body["recording"]["source_kind"] == "RemoteUrl"


def test_status_is_one_get(manager, transport):
    """T-IM-07."""
    handle = run(manager, Resolver(hour()))
    handle.result(3)
    assert asyncio.run(handle.status()) == ("importing", "binding_pending")
    assert transport.statuses == [handle.job_id]


def test_no_location_in_body(manager, transport):
    """T-IM-08."""
    run(manager, Resolver(hour())).result(3)
    raw = json.dumps(transport.imports[0][0])
    for forbidden in ("source_url", "provider_credential", "filename", "http", "/"):
        assert forbidden not in raw.replace("audio/wav", "")


def test_throttled_after_three_attempts(manager, transport):
    """T-IM-10."""
    transport.failures = [TransferError("throttled", True, 1)] * 3
    handle = run(manager, Resolver(hour()))
    assert handle.result(10).status == "throttled"
    assert len(transport.imports) <= 3


def test_default_resolver_unavailable(manager):
    assert run(manager, UnavailableResolver()).reason == "source_unavailable"
