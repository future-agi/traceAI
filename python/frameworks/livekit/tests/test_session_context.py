"""Call-local native LiveKit grouping, independent of attachment capture."""

import asyncio

import pytest
from fi_instrumentation import using_session
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.otel import GRPCSpanExporter, HTTPSpanExporter, Transport
from opentelemetry import context, trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_livekit import enable_grpc_attribute_mapping, enable_http_attribute_mapping
from traceai_livekit._session_context import _CONVERSATION, _SESSION, _SessionContextProcessor

SID = SpanAttributes.SESSION_ID
CID = SpanAttributes.GEN_AI_CONVERSATION_ID


@pytest.fixture
def mapped_spans(monkeypatch):
    spans = []

    def export(self, batch):
        spans.extend(batch)
        return SpanExportResult.SUCCESS

    # Exercise mapping, privacy and batch execution; replace only the HTTP send.
    monkeypatch.setattr(HTTPSpanExporter, "export", export)
    monkeypatch.setattr(GRPCSpanExporter, "export", export)
    return spans


@pytest.mark.parametrize("transport,enable", [
    (Transport.HTTP, enable_http_attribute_mapping),
    (Transport.GRPC, enable_grpc_attribute_mapping),
])
def test_capture_at_start_not_setup_or_batch_export(make_provider, mapped_spans, transport, enable):
    with using_session("setup-only"):
        provider = make_provider(transport)
        assert enable()
    # Context grouping does not depend on (or turn on) attachment capture.
    manager = getattr(provider, "attachment_manager", None)
    assert manager is None or manager.status().state == "disabled"
    tracer = provider.get_tracer("livekit-agents")
    with using_session("call"):
        span = tracer.start_span("agent_session")
    span.end()
    tracer.start_span("outside").end()
    with using_session("export-only"):
        provider.force_flush()
    by_name = {s.name: s.attributes for s in mapped_spans}
    assert by_name["agent_session"][SID] == "call"
    assert by_name["agent_session"][CID] == "call"
    assert SID not in by_name["outside"]
    assert CID not in by_name["outside"]


def test_installation_is_idempotent_and_native_scope_only(make_provider, mapped_spans):
    provider = make_provider()
    enable_http_attribute_mapping()
    enable_http_attribute_mapping()
    assert sum(isinstance(p, _SessionContextProcessor) for p in
               provider._active_span_processor._span_processors) == 1
    with using_session("native-only"):
        provider.get_tracer("application").start_span("unrelated").end()
    provider.force_flush()
    assert SID not in mapped_spans[0].attributes
    assert _SESSION not in mapped_spans[0].attributes


@pytest.mark.parametrize("separate_providers", [False, True])
def test_overlapping_calls_and_no_context_are_isolated(
    make_provider, mapped_spans, separate_providers,
):
    first = make_provider()
    enable_http_attribute_mapping()
    second = make_provider() if separate_providers else first
    if separate_providers:
        enable_http_attribute_mapping()
    tracers = [p.get_tracer("livekit-agents") for p in (first, second)]

    async def run():
        entered = [asyncio.Event(), asyncio.Event()]

        async def call(index):
            with using_session("call-" + str(index)):
                with tracers[index].start_as_current_span("root-" + str(index)):
                    entered[index].set()
                    await entered[1 - index].wait()
                    await asyncio.sleep(0)
                    tracers[index].start_span("child-" + str(index)).end()

        async def outside():
            await asyncio.gather(*(event.wait() for event in entered))
            tracers[0].start_span("outside").end()

        await asyncio.gather(call(0), call(1), outside())

    asyncio.run(run())
    first.force_flush()
    second.force_flush()
    by_name = {s.name: s for s in mapped_spans}
    for index in (0, 1):
        root, child = (by_name[prefix + str(index)] for prefix in ("root-", "child-"))
        assert root.attributes[SID] == child.attributes[SID] == "call-" + str(index)
        assert root.attributes[CID] == child.attributes[CID] == "call-" + str(index)
        assert child.parent.span_id == root.context.span_id
    assert SID not in by_name["outside"].attributes
    assert by_name["root-0"].context.trace_id != by_name["root-1"].context.trace_id


def test_explicit_parent_context_and_late_children(make_provider, mapped_spans):
    provider = make_provider()
    enable_http_attribute_mapping()
    tracer = provider.get_tracer("livekit-agents")
    with using_session("call"):
        root = tracer.start_span("agent_session")
        saved = context.get_current()
    # LiveKit starts shutdown work outside the entrypoint's Python context.
    with trace.use_span(root, end_on_exit=False):
        tracer.start_span("session_close").end()
    with using_session("unrelated"):
        tracer.start_span("explicit-context", context=saved).end()
        tracer.start_span("isolated-root", context=context.Context()).end()
    # A bare span id carries no session metadata; do not look it up globally.
    with trace.use_span(trace.NonRecordingSpan(root.context), end_on_exit=False):
        tracer.start_span("id-only-parent").end()
    root.end()
    provider.force_flush()
    by_name = {s.name: s.attributes for s in mapped_spans}
    for name in ("agent_session", "session_close", "explicit-context"):
        assert by_name[name][SID] == "call"
    for name in ("isolated-root", "id-only-parent"):
        assert SID not in by_name[name]


@pytest.mark.parametrize("attributes,expected_conversation", [
    ({SID: "explicit"}, "explicit"),
    ({SID: "explicit", CID: "explicit-conversation"}, "explicit-conversation"),
])
def test_explicit_ids_win_over_context_and_room(
    make_provider, mapped_spans, attributes, expected_conversation,
):
    provider = make_provider()
    enable_http_attribute_mapping()
    original = dict(attributes, **{"lk.pii.room_name": "room"})
    with using_session("ambient"):
        provider.get_tracer("livekit-agents").start_span("agent_session", attributes=original).end()
    provider.force_flush()
    assert mapped_spans[0].attributes[SID] == "explicit"
    assert mapped_spans[0].attributes[CID] == expected_conversation
    assert original == dict(attributes, **{"lk.pii.room_name": "room"})


@pytest.mark.parametrize("allow_pii", [True, False])
def test_privacy_filter_and_cloud_gate_do_not_receive_unqualified_copies(
    monkeypatch, make_provider, mapped_spans, allow_pii,
):
    from livekit.agents.telemetry.traces import _GatedSpanExporter

    monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", str(allow_pii).lower())
    provider = make_provider(batch=False)
    enable_http_attribute_mapping()
    raw = InMemorySpanExporter()
    cloud = InMemorySpanExporter()
    gate = _GatedSpanExporter(cloud, export_jobs={})
    gate.open_job("job")
    provider.add_span_processor(SimpleSpanProcessor(raw))
    provider.add_span_processor(SimpleSpanProcessor(gate))
    with using_session("private-call"):
        provider.get_tracer("livekit-agents").start_span("agent_session", attributes={"job_id": "job"}).end()
    gate.job_registered("job", traces_enabled=True, redacted=True)
    provider.force_flush()
    for span in (*raw.get_finished_spans(), *cloud.get_finished_spans()):
        assert SID not in span.attributes
        assert CID not in span.attributes
    cloud_attributes = cloud.get_finished_spans()[0].attributes
    assert _SESSION not in cloud_attributes
    assert _CONVERSATION not in cloud_attributes
    if allow_pii:
        assert mapped_spans[0].attributes[SID] == "private-call"
        assert raw.get_finished_spans()[0].attributes[_SESSION] == "private-call"
    else:
        assert SID not in mapped_spans[0].attributes
        assert CID not in mapped_spans[0].attributes
        assert _SESSION not in raw.get_finished_spans()[0].attributes


def test_project_redaction_wins_over_provider_permission(monkeypatch, make_provider, mapped_spans):
    from livekit.agents.types import ATTRIBUTE_REDACTION_ENABLED

    monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", "true")
    provider = make_provider()
    enable_http_attribute_mapping()
    with using_session("project-withheld"):
        span = provider.get_tracer("livekit-agents").start_span(
            "agent_session", attributes={ATTRIBUTE_REDACTION_ENABLED: True},
        )
    # End off the call context; the span's own project stamp still wins.
    span.end()
    provider.force_flush()
    assert SID not in mapped_spans[0].attributes
    assert CID not in mapped_spans[0].attributes
    assert not any("pii" in key.split(".") for key in mapped_spans[0].attributes)


def test_nested_context_restores_outer_session(make_provider, mapped_spans):
    provider = make_provider()
    enable_http_attribute_mapping()
    tracer = provider.get_tracer("livekit-agents")
    with using_session("outer"):
        with tracer.start_as_current_span("outer-root"):
            with using_session("inner"):
                tracer.start_span("inner-child").end()
            tracer.start_span("outer-child").end()
    tracer.start_span("outside").end()
    provider.force_flush()
    by_name = {s.name: s.attributes for s in mapped_spans}
    assert by_name["outer-root"][SID] == by_name["outer-child"][SID] == "outer"
    assert by_name["inner-child"][SID] == "inner"
    assert SID not in by_name["outside"]


def test_first_privacy_denial_wins_over_later_enable(monkeypatch, make_provider, mapped_spans):
    monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", "false")
    provider = make_provider()
    enable_http_attribute_mapping()
    monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", "true")
    enable_http_attribute_mapping()
    with using_session("withheld"):
        provider.get_tracer("livekit-agents").start_span("agent_session").end()
    provider.force_flush()
    assert SID not in mapped_spans[0].attributes
    assert CID not in mapped_spans[0].attributes
