"""S1 helper channels and S2 reserved-key export; application code is the principal.

Only transport sinks are replaced. Missing optional gRPC is an explicit skip,
not a gRPC compatibility pass; acceptance CI must provision that transport.
"""

from asyncio import CancelledError, run
from pathlib import Path
from types import SimpleNamespace

import pytest
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Sampler, SamplingResult

from traceai_livekit import MappedGRPCSpanExporter, MappedHTTPSpanExporter, record_egress
from traceai_livekit.exporters.base_exporter import BaseMappedSpanExporter


SOURCE = "lk.pii.recording.conversation_id"
PUBLIC = SpanAttributes.GEN_AI_CONVERSATION_ID
SESSION = SpanAttributes.SESSION_ID
ROOM = "person@example.invalid"
APP = "app-person@example.invalid"
REDACTED = "conversation-redacted-synthetic"
WITHHELD = "__REDACTED__"
BOUNDARY = "The recording helper controls only the metadata it writes. Attributes, events or links that your own samplers, span processors or code add are exported as written; the recording policy does not filter them, except the helper's reserved keys, which export always re-checks."


@pytest.fixture(params=["base", "http", "grpc"])
def concrete(request, monkeypatch):
    captured = []

    def sink(self, spans):
        captured.extend(spans)
        return SpanExportResult.SUCCESS

    if request.param == "base":
        class Sink(SpanExporter):
            export = sink

        class MappedSink(BaseMappedSpanExporter, Sink):
            pass

        exporter = MappedSink()
    else:
        import fi_instrumentation.otel as fi_otel

        # No environment credentials are read, even by FI's gRPC constructor.
        monkeypatch.setattr(fi_otel, "get_env_fi_auth_header", lambda: {})
        if request.param == "http":
            wrapped = fi_otel.HTTPSpanExporter
            cls = MappedHTTPSpanExporter
            options = {"endpoint": "https://collector.example.invalid/v1/traces"}
        else:
            pytest.importorskip(
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
                reason="optional OTLP gRPC transport missing; provision it for acceptance",
            )
            wrapped = fi_otel.GRPCSpanExporter
            cls = MappedGRPCSpanExporter
            options = {"endpoint": "collector.example.invalid:4317", "insecure": True}
        # export is inherited from the actual OTLP transport, not defined by FI.
        owner = next((base for base in wrapped.__mro__ if "export" in vars(base)), None)
        assert owner is not None, "FI exporter has no real transport export entry point"
        monkeypatch.setattr(owner, "export", sink)
        exporter = cls(headers={"authorization": "egress-synthetic"}, **options)
    yield SimpleNamespace(exporter=exporter, captured=captured, transport=request.param)
    exporter.shutdown()


@pytest.mark.parametrize("marker", [True, False], ids=["marker", "marker-lost"])
@pytest.mark.parametrize("conversation", [None, REDACTED], ids=["withheld", "redacted"])
def test_concrete_recording_mapping_preserves_original(concrete, marker, conversation, caplog):
    provider = TracerProvider()
    raw = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(raw))
    span = provider.get_tracer("caller-synthetic").start_span(
        "livekit.recording.snapshot", attributes={
            "lk.pii.recording.egress_id": "egress-synthetic",
            "lk.recording.status": "0", "lk.pii.room_name": ROOM,
            "lk.pii.recording.file_locations": ("https://storage.example.invalid/call.mp4",),
            PUBLIC: "stale-person@example.invalid",
        },
    )
    if marker:
        span.set_attribute("lk.recording.operation", "snapshot")
    if conversation is not None:
        span.set_attribute(SOURCE, conversation)
    span.end()
    (readable,) = raw.get_finished_spans()
    original = dict(readable.attributes)
    try:
        assert concrete.exporter.export([readable]) is SpanExportResult.SUCCESS
        (mapped,) = concrete.captured
        assert mapped is not readable
        assert mapped.attributes["livekit.recording.egress_id"] == "egress-synthetic"
        assert mapped.attributes["livekit.recording.status"] == "0"
        assert mapped.attributes["livekit.recording.room_name"] == ROOM
        assert mapped.attributes["livekit.recording.file_locations"] == (
            "https://storage.example.invalid/call.mp4",
        )
        assert ("livekit.recording.operation" in mapped.attributes) is marker
        if conversation is None:
            assert PUBLIC not in mapped.attributes
        else:
            assert mapped.attributes[PUBLIC] == conversation
        assert dict(readable.attributes) == original
        assert "could not map" not in caplog.text
        assert concrete.exporter._convert_attributes({"lk.pii.room_name": ROOM})[PUBLIC] == ROOM
    finally:
        provider.shutdown()
        raw.shutdown()


class ConversationPolicy(TraceConfig):
    def __init__(self, replacement):
        super().__init__()
        object.__setattr__(self, "replacement", replacement)

    def mask(self, key, value):
        if key in (PUBLIC, SESSION):
            return self.replacement
        return super().mask(key, value)


def _parent():
    return trace.NonRecordingSpan(trace.SpanContext(1, 2, False, trace.TraceFlags(1)))


def _capture(tracer, policy, **kwargs):
    return record_egress(
        SimpleNamespace(egress_id="egress-synthetic", room_name=ROOM, status=0),
        session_span=_parent(), tracer=tracer, trace_config=policy, **kwargs,
    )


def _readable_copy(span):
    return ReadableSpan(**{name: getattr(span, name) for name in (
        "name", "context", "parent", "resource", "attributes", "events", "links",
        "kind", "status", "start_time", "end_time", "instrumentation_scope",
    )})


class Inject(SpanProcessor):
    def on_start(self, span, parent_context=None):
        # An application can use the public context-manager entry or any writer.
        writable = span.__enter__()
        writable.set_attributes({SOURCE: APP, PUBLIC: APP, SESSION: APP, "app.extra": APP})
        writable.add_event("app-event-synthetic", {SOURCE: APP, PUBLIC: APP, SESSION: APP, "app.extra": APP})
        writable.record_exception(ValueError("app-exception-synthetic"), attributes={PUBLIC: APP, "app.extra": APP})
        if hasattr(writable, "add_link"):
            writable.add_link(_parent().get_span_context(), {PUBLIC: APP, SESSION: APP, "app.extra": APP})

    def on_end(self, span):
        pass


class InjectSampler(Sampler):
    def should_sample(self, parent_context, trace_id, name, kind=None,
                      attributes=None, links=None, trace_state=None):
        result = ALWAYS_ON.should_sample(parent_context, trace_id, name, kind,
                                        attributes, links, trace_state)
        selected = dict(result.attributes or {})
        selected.pop("lk.recording.operation", None)
        selected.update({SOURCE: APP, PUBLIC: APP, SESSION: APP, "app.extra": APP})
        return SamplingResult(result.decision, selected, trace.TraceState([("recording", "synthetic")]))

    def get_description(self):
        return "recording-synthetic"


@pytest.mark.parametrize("writer", ["processor", "sampler"])
@pytest.mark.parametrize("owner", ["helper", "weak-fi-helper", "strong-fi"])
@pytest.mark.parametrize("replacement", [None, REDACTED], ids=["withheld", "redacted"])
def test_reserved_keys_are_rechecked_at_export_regardless_of_writer(concrete, writer, owner, replacement):
    provider = TracerProvider(sampler=InjectSampler() if writer == "sampler" else ALWAYS_ON)
    raw = InMemorySpanExporter()
    if writer == "processor":
        provider.add_span_processor(Inject())
    provider.add_span_processor(SimpleSpanProcessor(raw))
    tracer = provider.get_tracer("caller-synthetic")
    original_sampler, original_processor = tracer.sampler, tracer.span_processor
    policy = ConversationPolicy(replacement)
    selected, helper = tracer, policy
    if owner == "weak-fi-helper":
        selected = FITracer(tracer, config=TraceConfig())
    elif owner == "strong-fi":
        selected, helper = FITracer(tracer, config=policy), TraceConfig()
    before = context.get_current()
    try:
        assert _capture(selected, helper)
        (original,) = raw.get_finished_spans()
        # Application additions remain on the original; no provenance/guard promise.
        for key in (SOURCE, PUBLIC, SESSION, "app.extra"):
            assert original.attributes[key] == APP
        assert concrete.exporter.export([original]) is SpanExportResult.SUCCESS
        (mapped,) = concrete.captured
        assert mapped.attributes["app.extra"] == APP
        for attrs in [mapped.attributes, *(e.attributes for e in mapped.events),
                      *(link.attributes for link in mapped.links)]:
            for key in (SOURCE, PUBLIC, SESSION):
                if key in attrs:
                    assert replacement is not None and attrs[key] == replacement
            if "app.extra" in attrs:
                assert attrs["app.extra"] == APP
        if replacement is not None:
            assert mapped.attributes[SOURCE] == mapped.attributes[PUBLIC] == mapped.attributes[SESSION] == replacement
        assert [(e.name, e.timestamp) for e in mapped.events] == [(e.name, e.timestamp) for e in original.events]
        assert [link.context for link in mapped.links] == [link.context for link in original.links]
        assert original.attributes[SOURCE] == APP
        assert tracer.sampler is original_sampler and tracer.span_processor is original_processor
        assert context.get_current() is before
    finally:
        provider.shutdown()


RESERVED_PAIRS = [
    ("lk.recording.operation", "livekit.recording.operation"),
    ("lk.pii.recording.egress_id", "livekit.recording.egress_id"),
    ("lk.recording.status", "livekit.recording.status"),
    ("lk.recording.started_at_unix_nano", "livekit.recording.started_at_unix_nano"),
    ("lk.recording.ended_at_unix_nano", "livekit.recording.ended_at_unix_nano"),
    ("lk.pii.room_name", "livekit.recording.room_name"),
    (SOURCE, PUBLIC),
    ("lk.pii.recording.file_locations", "livekit.recording.file_locations"),
    ("lk.pii.recording.stream_locations", "livekit.recording.stream_locations"),
]


@pytest.mark.parametrize("source,alias", RESERVED_PAIRS, ids=[pair[0] for pair in RESERVED_PAIRS])
@pytest.mark.parametrize("gate", ["source", "destination"])
def test_every_reserved_recording_key_obeys_destination_policy(concrete, source, alias, gate):
    class Policy(TraceConfig):
        armed = False

        def mask(self, key, value):
            if self.armed and key == (source if gate == "source" else alias):
                return None
            return super().mask(key, value)

    policy = Policy()

    class Add(SpanProcessor):
        def on_start(self, span, parent_context=None):
            span.__enter__().set_attributes({source: APP, alias: APP, "app.extra": APP})
            object.__setattr__(policy, "armed", True)

        def on_end(self, span):
            pass

    provider = TracerProvider()
    raw = InMemorySpanExporter()
    provider.add_span_processor(Add())
    provider.add_span_processor(SimpleSpanProcessor(raw))
    try:
        assert _capture(provider.get_tracer("caller-synthetic"), policy)
        (original,) = raw.get_finished_spans()
        assert original.attributes[source] == APP
        concrete.exporter.export([original])
        (mapped,) = concrete.captured
        assert source not in mapped.attributes and alias not in mapped.attributes
        assert mapped.attributes["app.extra"] == APP
        assert original.attributes[source] == APP
    finally:
        provider.shutdown()


@pytest.mark.parametrize("replacement", [None, REDACTED], ids=["withheld", "redacted"])
def test_helper_withheld_values_are_absent_from_all_observable_channels(concrete, replacement, caplog):
    from fi_instrumentation import using_session

    seen = []

    class Observe(SpanProcessor):
        def snapshot(self, span, parent_context=None):
            seen.append({
                "name": span.name, "attributes": dict(span.attributes or {}),
                "events": [(e.name, dict(e.attributes or {})) for e in span.events],
                "links": [(str(link.context), dict(link.attributes or {})) for link in span.links],
                "status": str(span.status), "resource": dict(span.resource.attributes),
                "scope": str(span.instrumentation_scope), "context": dict(context.get_current()),
                "baggage": dict(baggage.get_all()), "parent_context": dict(parent_context or {}),
            })

        def on_start(self, span, parent_context=None):
            self.snapshot(span, parent_context)

        def on_end(self, span):
            self.snapshot(span)

    provider = TracerProvider()
    provider.add_span_processor(Observe())
    provider.add_span_processor(SimpleSpanProcessor(concrete.exporter))
    tracer = provider.get_tracer("caller-synthetic")
    token = context.attach(baggage.set_baggage("ambient", "ambient-baggage@example.invalid"))
    try:
        with using_session("ambient-session@example.invalid"):
            before = context.get_current()
            result = _capture(FITracer(tracer, config=TraceConfig()), ConversationPolicy(replacement),
                              conversation_id="helper-withheld@example.invalid")
            assert result is True
            assert context.get_current() is before
        assert len(seen) == 2
        text = str(seen) + str([dict(s.attributes) for s in concrete.captured]) + caplog.text
        for withheld in ("helper-withheld@example.invalid", "ambient-baggage@example.invalid", "ambient-session@example.invalid"):
            assert withheld not in text
        for observation in seen:
            assert not observation["events"] and not observation["links"]
            assert not observation["baggage"]
            assert observation["attributes"].get(SOURCE) == replacement
    finally:
        context.detach(token)
        provider.shutdown()


@pytest.mark.parametrize("delivery", ["copy", "cloud-redacted"])
def test_export_policy_survives_delayed_and_redacted_span_copies(concrete, delivery):
    from livekit.agents.telemetry.traces import _GatedSpanExporter

    class Add(SpanProcessor):
        def on_start(self, span, parent_context=None):
            span.__enter__().set_attributes({SESSION: APP, "app.extra": APP, "job_id": "egress-synthetic"})

        def on_end(self, span):
            pass

    provider = TracerProvider()
    raw = InMemorySpanExporter()
    provider.add_span_processor(Add())
    provider.add_span_processor(SimpleSpanProcessor(raw))
    try:
        assert _capture(provider.get_tracer("caller-synthetic"), ConversationPolicy(None))
        (original,) = raw.get_finished_spans()
        copied = _readable_copy(original)
        if delivery == "cloud-redacted":
            cloud = InMemorySpanExporter()
            gate = _GatedSpanExporter(cloud, export_jobs={})
            gate.open_job("egress-synthetic")
            gate.export([copied])
            gate.job_registered("egress-synthetic", traces_enabled=True, redacted=True)
            (copied,) = cloud.get_finished_spans()
            assert "lk.pii.room_name" not in copied.attributes
        # Export after record_egress returned, with no attached recording context.
        concrete.exporter.export([copied])
        (mapped,) = concrete.captured
        assert SESSION not in mapped.attributes
        assert mapped.attributes["app.extra"] == APP
        assert original.attributes[SESSION] == APP
    finally:
        provider.shutdown()


def test_recording_policies_are_local_to_each_call(concrete):
    provider = TracerProvider(sampler=InjectSampler())
    raw = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(raw))
    tracer = provider.get_tracer("caller-synthetic")
    selected = FITracer(tracer, config=TraceConfig())
    original_config = selected._self_config
    try:
        for policy in (ConversationPolicy(None), TraceConfig()):
            assert _capture(selected, policy)
        first, second = raw.get_finished_spans()
        # Reverse-order, delayed exports must not pick up the last helper's policy.
        concrete.exporter.export([_readable_copy(second), _readable_copy(first)])
        allowed, denied = concrete.captured
        assert allowed.attributes[PUBLIC] == allowed.attributes[SESSION] == APP
        assert SOURCE not in denied.attributes and PUBLIC not in denied.attributes and SESSION not in denied.attributes
        assert first.attributes[SOURCE] == second.attributes[SOURCE] == APP
        assert selected._self_config is original_config
    finally:
        provider.shutdown()


def test_processor_routing_policy_exception_no_longer_abandons_child(concrete, caplog):
    class Policy(TraceConfig):
        def mask(self, key, value):
            if key == "job_id":
                raise RuntimeError("routing-withheld@example.invalid")
            return super().mask(key, value)

    started = []

    class AddJob(SpanProcessor):
        def on_start(self, span, parent_context=None):
            started.append(span)
            span.set_attribute("job_id", "egress-synthetic")

        def on_end(self, span):
            pass

    provider = TracerProvider()
    provider.add_span_processor(AddJob())
    provider.add_span_processor(SimpleSpanProcessor(concrete.exporter))
    try:
        assert _capture(provider.get_tracer("caller-synthetic"), Policy())
        assert len(started) == 1 and not started[0].is_recording()
        assert len(concrete.captured) == 1 and concrete.captured[0].end_time is not None
        assert concrete.captured[0].attributes["job_id"] == "egress-synthetic"
        assert "routing-withheld@example.invalid" not in caplog.text
    finally:
        provider.shutdown()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt], ids=["exception", "baseexception"])
def test_started_child_is_ended_when_start_raises(concrete, error_type, caplog):
    started = []

    class FailAfterAllocation(SpanProcessor):
        def on_start(self, span, parent_context=None):
            started.append(span)
            raise error_type("start-withheld@example.invalid")

        def on_end(self, span):
            pass

    provider = TracerProvider()
    provider.add_span_processor(FailAfterAllocation())
    provider.add_span_processor(SimpleSpanProcessor(concrete.exporter))
    before = context.get_current()
    try:
        if error_type is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt):
                _capture(provider.get_tracer("caller-synthetic"), TraceConfig())
        else:
            assert _capture(provider.get_tracer("caller-synthetic"), TraceConfig()) is False
        assert len(started) == 1 and not started[0].is_recording()
        assert len(concrete.captured) == 1 and concrete.captured[0].end_time is not None
        assert context.get_current() is before
        assert "start-withheld@example.invalid" not in caplog.text
    finally:
        provider.shutdown()


def test_recording_export_mask_exception_uses_withheld_marker(concrete, caplog):
    class Policy(TraceConfig):
        def mask(self, key, value):
            if key in (PUBLIC, SESSION) and value == APP:
                raise RuntimeError(APP)
            return super().mask(key, value)

    provider = TracerProvider()
    provider.add_span_processor(Inject())
    raw = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(raw))
    provider.add_span_processor(SimpleSpanProcessor(concrete.exporter))
    try:
        assert _capture(provider.get_tracer("caller-synthetic"), Policy())
        (original,) = raw.get_finished_spans()
        (mapped,) = concrete.captured
        assert original.end_time is not None
        for key in (SOURCE, PUBLIC, SESSION):
            assert mapped.attributes[key] == WITHHELD
        for item in (*mapped.events, *mapped.links):
            for key in (SOURCE, PUBLIC, SESSION):
                if key in item.attributes:
                    assert item.attributes[key] == WITHHELD
        assert mapped.attributes["app.extra"] == APP
        assert APP not in caplog.text
    finally:
        provider.shutdown()


def test_recording_boundary_fails_closed_for_unknown_tracer():
    calls = []

    class UnknownTracer:
        def start_span(self, *args, **kwargs):
            calls.append((args, kwargs))
            return trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)

    before = context.get_current()
    assert _capture(UnknownTracer(), ConversationPolicy(None)) is False
    assert calls == []
    assert context.get_current() is before


def test_recording_docs_state_the_scope_boundary():
    root = Path(__file__).resolve().parents[1]
    for filename in ("README.md", "CHANGELOG.md"):
        assert BOUNDARY in (root / filename).read_text()
    assert BOUNDARY in record_egress.__doc__


def test_batch_export_keeps_each_recording_policy(concrete):
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(sampler=InjectSampler())
    provider.add_span_processor(BatchSpanProcessor(concrete.exporter, schedule_delay_millis=60000))
    tracer = provider.get_tracer("caller-synthetic")
    try:
        assert _capture(tracer, ConversationPolicy(None))
        assert _capture(tracer, TraceConfig())
        assert provider.force_flush()
        assert len(concrete.captured) == 2
        denied, allowed = concrete.captured
        assert PUBLIC not in denied.attributes and SESSION not in denied.attributes
        assert allowed.attributes[PUBLIC] == allowed.attributes[SESSION] == APP
        assert denied.attributes["app.extra"] == allowed.attributes["app.extra"] == APP
    finally:
        provider.shutdown()


def test_recording_policy_is_not_serialized_into_otlp_scope(concrete):
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(concrete.exporter))
    tracer = provider.get_tracer("caller-synthetic", "1", schema_url="https://schema.example.invalid")
    try:
        assert _capture(tracer, ConversationPolicy(None), conversation_id="helper-withheld@example.invalid")
        request = encode_spans(concrete.captured)
        encoded = request.SerializeToString()
        assert b"helper-withheld@example.invalid" not in encoded
        assert b"recording_policies" not in encoded
        scope_spans = request.resource_spans[0].scope_spans[0]
        assert scope_spans.scope.name == "caller-synthetic"
        assert scope_spans.scope.version == "1"
        assert scope_spans.schema_url == "https://schema.example.invalid"
        assert not scope_spans.scope.attributes
    finally:
        provider.shutdown()


def test_reserved_event_keys_are_rechecked_with_zero_span_attributes(concrete):
    from opentelemetry.sdk.trace import SpanLimits

    provider = TracerProvider(span_limits=SpanLimits(
        max_attributes=0, max_event_attributes=128, max_link_attributes=128,
    ))
    provider.add_span_processor(Inject())
    provider.add_span_processor(SimpleSpanProcessor(concrete.exporter))
    try:
        assert _capture(provider.get_tracer("caller-synthetic"), ConversationPolicy(None))
        (mapped,) = concrete.captured
        assert not mapped.attributes
        assert mapped.events
        for event in mapped.events:
            assert PUBLIC not in event.attributes and SOURCE not in event.attributes and SESSION not in event.attributes
            assert event.attributes["app.extra"] == APP
    finally:
        provider.shutdown()


def test_recording_mapping_fallback_still_rechecks_reserved_keys(concrete, monkeypatch, caplog):
    from traceai_livekit.exporters import base_exporter

    def fail_mapping(*args, **kwargs):
        raise RuntimeError(APP)

    monkeypatch.setattr(base_exporter, "_map_attributes_to_fi_conventions", fail_mapping)
    provider = TracerProvider(sampler=InjectSampler())
    provider.add_span_processor(SimpleSpanProcessor(concrete.exporter))
    try:
        assert _capture(provider.get_tracer("caller-synthetic"), ConversationPolicy(None))
        (mapped,) = concrete.captured
        assert PUBLIC not in mapped.attributes and SOURCE not in mapped.attributes and SESSION not in mapped.attributes
        assert mapped.attributes["app.extra"] == APP
        assert APP not in caplog.text
    finally:
        provider.shutdown()


def test_recording_copy_failure_never_exports_the_original(concrete, monkeypatch, caplog):
    def fail_copy(span):
        raise RuntimeError(APP)

    # Marker-only classification is also recording, not just reserved names.
    original = ReadableSpan(
        name="caller-synthetic", context=_parent().get_span_context(),
        attributes={"lk.recording.operation": "snapshot", SOURCE: APP},
    )
    monkeypatch.setattr(concrete.exporter, "_mapped_copy", fail_copy)
    assert concrete.exporter.export([original]) is SpanExportResult.FAILURE
    assert concrete.captured == []
    assert original.attributes[SOURCE] == APP
    assert APP not in caplog.text


INTERRUPTS = [
    pytest.param(KeyboardInterrupt, id="keyboard"),
    pytest.param(CancelledError, id="cancelled"),
    pytest.param(SystemExit, id="system-exit"),
    pytest.param(GeneratorExit, id="generator-exit"),
]


@pytest.mark.parametrize("gate", [SOURCE, PUBLIC], ids=["source", "destination"])
def test_transient_helper_mask_failure_withholds_source_and_alias(concrete, gate, caplog):
    class Policy(TraceConfig):
        armed = False
        calls = 0

        def mask(self, key, value):
            if self.armed and key == gate:
                object.__setattr__(self, "calls", self.calls + 1)
                if self.calls == 1:
                    raise RuntimeError("mask-failure@example.invalid")
            return super().mask(key, value)

    policy = Policy()
    provider = TracerProvider()
    raw = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(raw))
    try:
        # No application writer: this is the helper's implicit conversation.
        assert _capture(provider.get_tracer("caller-synthetic"), policy)
        (original,) = raw.get_finished_spans()
        object.__setattr__(policy, "armed", True)
        assert concrete.exporter.export([original]) is SpanExportResult.SUCCESS
        (mapped,) = concrete.captured
        assert mapped.attributes[SOURCE] == mapped.attributes[PUBLIC] == WITHHELD
        assert policy.calls == 1, "a failed family must not retry its raw alias"
        assert original.attributes[SOURCE] == ROOM and PUBLIC not in original.attributes
        assert "mask-failure@example.invalid" not in caplog.text
    finally:
        provider.shutdown()


@pytest.mark.parametrize("channel", ["span", "event", "link"])
@pytest.mark.parametrize("gate", ["source", "destination"])
@pytest.mark.parametrize("source,alias", RESERVED_PAIRS, ids=[pair[0] for pair in RESERVED_PAIRS])
def test_transient_reserved_family_failure_is_atomic(concrete, source, alias, gate, channel):
    from opentelemetry.sdk.trace import Event
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from traceai_livekit._recording_boundary import _RecordingScope

    class Policy(TraceConfig):
        calls = 0

        def mask(self, key, value):
            if key == (source if gate == "source" else alias):
                object.__setattr__(self, "calls", self.calls + 1)
                if self.calls == 1:
                    raise RuntimeError("mask-failure@example.invalid")
            return value

    policy = Policy()
    # Alias-first ordering must not change the result. Unrelated keys are controls.
    attributes = {alias: "egress-synthetic", source: "egress-synthetic", "app.extra": APP}
    original = ReadableSpan(
        name="livekit.recording.snapshot", context=_parent().get_span_context(),
        attributes=attributes if channel == "span" else {},
        events=[Event("event-synthetic", attributes)] if channel == "event" else [],
        links=[trace.Link(_parent().get_span_context(), attributes)] if channel == "link" else [],
        instrumentation_scope=_RecordingScope(InstrumentationScope("caller-synthetic"), (policy,)),
    )
    assert concrete.exporter.export([original]) is SpanExportResult.SUCCESS
    (mapped,) = concrete.captured
    if channel == "span":
        checked, unchanged = mapped.attributes, original.attributes
    elif channel == "event":
        checked, unchanged = mapped.events[0].attributes, original.events[0].attributes
    else:
        checked, unchanged = mapped.links[0].attributes, original.links[0].attributes
    expected = (WITHHELD,) if source.endswith("_locations") else WITHHELD
    assert checked[source] == checked[alias] == expected
    assert checked["app.extra"] == APP
    assert unchanged[source] == unchanged[alias] == "egress-synthetic"
    assert policy.calls == 1


@pytest.mark.parametrize("fail", [False, True], ids=["success", "alias-failure"])
def test_distinct_reserved_alias_values_survive_successful_recheck(concrete, fail):
    from opentelemetry.sdk.trace import Event
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from traceai_livekit._recording_boundary import _RecordingScope

    class Policy(TraceConfig):
        def mask(self, key, value):
            if fail and key == PUBLIC and value == APP:
                raise RuntimeError("mask-failure@example.invalid")
            return value

    attributes = {SOURCE: ROOM, PUBLIC: APP, "app.extra": APP}
    original = ReadableSpan(
        name="livekit.recording.snapshot", context=_parent().get_span_context(),
        events=[Event("event-synthetic", attributes)],
        links=[trace.Link(_parent().get_span_context(), attributes)],
        instrumentation_scope=_RecordingScope(InstrumentationScope("caller-synthetic"), (Policy(),)),
    )
    assert concrete.exporter.export([original]) is SpanExportResult.SUCCESS
    (mapped,) = concrete.captured
    for item in (*mapped.events, *mapped.links):
        assert item.attributes[SOURCE] == (WITHHELD if fail else ROOM)
        assert item.attributes[PUBLIC] == (WITHHELD if fail else APP)
        assert item.attributes["app.extra"] == APP
    for item in (*original.events, *original.links):
        assert item.attributes[SOURCE] == ROOM and item.attributes[PUBLIC] == APP


def test_room_mask_failure_withholds_implicit_conversation(concrete):
    class Policy(TraceConfig):
        armed = False
        failed = False

        def mask(self, key, value):
            if self.armed and key == "livekit.recording.room_name" and not self.failed:
                object.__setattr__(self, "failed", True)
                raise RuntimeError("mask-failure@example.invalid")
            return value

    policy = Policy()
    provider = TracerProvider()
    raw = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(raw))
    try:
        assert _capture(provider.get_tracer("caller-synthetic"), policy)
        (original,) = raw.get_finished_spans()
        object.__setattr__(policy, "armed", True)
        assert concrete.exporter.export([original]) is SpanExportResult.SUCCESS
        (mapped,) = concrete.captured
        for key in ("lk.pii.room_name", "livekit.recording.room_name", SOURCE, PUBLIC):
            assert mapped.attributes[key] == WITHHELD
        assert original.attributes[SOURCE] == original.attributes["lk.pii.room_name"] == ROOM
    finally:
        provider.shutdown()


@pytest.mark.parametrize("error_type", INTERRUPTS)
def test_start_interrupt_survives_ordinary_end_failure(error_type, caplog):
    interruption = error_type("interrupt-synthetic")
    started, ended = [], []

    class FailingCallbacks(SpanProcessor):
        def on_start(self, span, parent_context=None):
            started.append(span)
            raise interruption

        def on_end(self, span):
            ended.append(span)
            raise RuntimeError("cleanup-failure@example.invalid")

    provider = TracerProvider()
    provider.add_span_processor(FailingCallbacks())
    before = context.get_current()
    try:
        with pytest.raises(error_type) as caught:
            _capture(provider.get_tracer("caller-synthetic"), TraceConfig())
        assert caught.value is interruption
        assert any(frame.name == "on_start" for frame in caught.traceback)
        assert len(started) == len(ended) == 1
        assert not started[0].is_recording() and ended[0].end_time is not None
        assert context.get_current() is before
        assert "cleanup-failure@example.invalid" not in caplog.text
    finally:
        provider.shutdown()


@pytest.mark.parametrize("error_type", INTERRUPTS)
@pytest.mark.parametrize("origin", ["start", "end"])
def test_interrupt_survives_ordinary_context_cleanup_failure(error_type, origin, monkeypatch):
    import traceai_livekit.recording as recording_module

    interruption = error_type("interrupt-synthetic")
    ended, detached = [], []

    class Interrupt(SpanProcessor):
        def on_start(self, span, parent_context=None):
            if origin == "start":
                raise interruption

        def on_end(self, span):
            ended.append(span)
            if origin == "end":
                raise interruption

    provider = TracerProvider()
    provider.add_span_processor(Interrupt())
    detach = context.detach
    before = context.get_current()

    def failing_detach(token):
        detach(token)
        detached.append(token)
        raise RuntimeError("context-cleanup@example.invalid")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(recording_module.context, "detach", failing_detach)
            with pytest.raises(error_type) as caught:
                _capture(provider.get_tracer("caller-synthetic"), TraceConfig())
        assert caught.value is interruption
        assert len(ended) == len(detached) == 1
        assert context.get_current() is before
    finally:
        provider.shutdown()


@pytest.mark.parametrize("error_type", INTERRUPTS)
@pytest.mark.parametrize("start_failure", ["none", "ordinary", "interrupt"])
def test_cleanup_interrupt_always_propagates(error_type, start_failure):
    interruption = error_type("cleanup-interrupt-synthetic")
    ended = []

    class Interrupt(SpanProcessor):
        def on_start(self, span, parent_context=None):
            if start_failure == "ordinary":
                raise RuntimeError("start-failure@example.invalid")
            if start_failure == "interrupt":
                raise KeyboardInterrupt("earlier-interrupt-synthetic")

        def on_end(self, span):
            ended.append(span)
            raise interruption

    provider = TracerProvider()
    provider.add_span_processor(Interrupt())
    before = context.get_current()
    try:
        with pytest.raises(error_type) as caught:
            _capture(provider.get_tracer("caller-synthetic"), TraceConfig())
        assert caught.value is interruption
        assert len(ended) == 1 and ended[0].end_time is not None
        assert context.get_current() is before
    finally:
        provider.shutdown()


def test_ordinary_end_failure_still_returns_false(caplog):
    class FailEnd(SpanProcessor):
        def on_start(self, span, parent_context=None):
            pass

        def on_end(self, span):
            raise RuntimeError("cleanup-failure@example.invalid")

    provider = TracerProvider()
    provider.add_span_processor(FailEnd())
    before = context.get_current()
    try:
        # The caller may itself be handling an interrupt. Only this call's
        # pending exception, not ambient sys.exc_info(), determines precedence.
        try:
            raise KeyboardInterrupt("caller-interrupt-synthetic")
        except KeyboardInterrupt:
            assert _capture(provider.get_tracer("caller-synthetic"), TraceConfig()) is False
        assert context.get_current() is before
        assert "cleanup-failure@example.invalid" not in caplog.text
    finally:
        provider.shutdown()


@pytest.mark.parametrize("error_type", INTERRUPTS)
def test_mask_baseexception_propagates_before_child_creation(error_type):
    interruption = error_type("mask-interrupt-synthetic")
    started = []

    class Policy(TraceConfig):
        def mask(self, key, value):
            if key == PUBLIC:
                raise interruption
            return value

    class Observe(SpanProcessor):
        def on_start(self, span, parent_context=None):
            started.append(span)

        def on_end(self, span):
            pass

    provider = TracerProvider()
    provider.add_span_processor(Observe())
    before = context.get_current()
    try:
        with pytest.raises(error_type) as caught:
            _capture(provider.get_tracer("caller-synthetic"), Policy())
        assert caught.value is interruption and not started
        assert context.get_current() is before
    finally:
        provider.shutdown()


@pytest.mark.parametrize("error_type", INTERRUPTS)
def test_wrapped_capture_preserves_interrupt_after_successful_delegate(error_type):
    from traceai_livekit import wrap_egress_client

    interruption = error_type("interrupt-synthetic")
    calls, ended = [], []
    response = SimpleNamespace(egress_id="egress-synthetic", room_name=ROOM)

    class Service:
        async def start_room_composite_egress(self, request):
            calls.append(request)
            return response

    class Interrupt(SpanProcessor):
        def on_start(self, span, parent_context=None):
            raise interruption

        def on_end(self, span):
            ended.append(span)
            raise RuntimeError("cleanup-failure@example.invalid")

    provider = TracerProvider()
    provider.add_span_processor(Interrupt())
    before = context.get_current()
    try:
        proxy = wrap_egress_client(Service(), session_span=_parent(),
                                   tracer=provider.get_tracer("caller-synthetic"))
        request = object()

        async def invoke():
            inside_before = context.get_current()
            with pytest.raises(error_type) as caught:
                await proxy.start_room_composite_egress(request)
            assert caught.value is interruption
            assert context.get_current() is inside_before

        run(invoke())
        assert calls == [request] and len(ended) == 1
        assert context.get_current() is before
    finally:
        provider.shutdown()


@pytest.mark.parametrize("fail_at", [1, 2, 3], ids=["span-failure", "event-failure", "link-failure"])
def test_transient_family_failure_withholds_all_export_channels(concrete, fail_at):
    from opentelemetry.sdk.trace import Event
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from traceai_livekit._recording_boundary import _RecordingScope

    class Policy(TraceConfig):
        calls = 0

        def mask(self, key, value):
            if key == PUBLIC:
                object.__setattr__(self, "calls", self.calls + 1)
                if self.calls == fail_at:
                    raise RuntimeError("mask-failure@example.invalid")
            return value

    policy = Policy()
    attributes = {SOURCE: ROOM, PUBLIC: ROOM, "app.extra": APP}
    original = ReadableSpan(
        name="livekit.recording.snapshot", context=_parent().get_span_context(),
        attributes=attributes, events=[Event("event-synthetic", attributes)],
        links=[trace.Link(_parent().get_span_context(), attributes)],
        instrumentation_scope=_RecordingScope(InstrumentationScope("caller-synthetic"), (policy,)),
    )
    assert concrete.exporter.export([original]) is SpanExportResult.SUCCESS
    (mapped,) = concrete.captured
    for checked in (mapped.attributes, mapped.events[0].attributes, mapped.links[0].attributes):
        assert checked[SOURCE] == checked[PUBLIC] == WITHHELD
        assert checked["app.extra"] == APP
    assert policy.calls == fail_at, "no raw family retry after its first exception"
    for unchanged in (original.attributes, original.events[0].attributes, original.links[0].attributes):
        assert unchanged[SOURCE] == unchanged[PUBLIC] == ROOM
