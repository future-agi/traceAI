"""Round 6 pass A (r6a verifier, second pass, BLOCKING #1): media context lost before recording.

The admission recorder is installed at ``on_start``, but the SDK admits constructor-supplied
span attributes and link attributes before that callback, and builds event/``add_link``
attribute containers with no callback at all; a span that reaches a traceAI exporter through
a host-owned processor never meets an FI start hook. In each case oldest-first eviction could
drop a MIME sibling while its short PCM payload stayed, and the payload exported raw.

The invariant now: a container is sanitized with its recorded media context only when that
record covers every key it lost. Otherwise (``dropped`` keys nobody recorded) the one S1
sanitizer treats the container as media context unknown and fails closed for it: every
catalog payload field is omitted, identity/metadata/timing are kept. Sanitized snapshots are
stamped as fully accounted, so a second boundary never re-omits already-clean content.

Synthetic payloads and loopback collectors only.
"""

import json
from typing import Any, Dict, List

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer, TraceConfig, register
from fi_instrumentation import _attribute_limits as _limits
from fi_instrumentation.auto import _manager
from fi_instrumentation.auto.wire._privacy import export_span
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_round6a import _Collector

PCM = "AQIDBA=="  # 4 raw PCM bytes: no magic, recognisable only through its MIME sibling
OMITTED = "<omitted: 8 base64 chars>"
MIME = "input.parts.0.mime_type"
CONTENT = "input.parts.0.content"
LIMITS = [(128, 140), (32, 40), (8, 20)]


def _media(filler: int) -> Dict[str, Any]:
    """MIME first, ``filler`` metadata attributes, then the payload (the SDK evicts oldest first)."""
    attrs: Dict[str, Any] = {MIME: "audio/pcm"}
    for i in range(filler):
        attrs[f"meta.{i:03d}"] = i
    attrs[CONTENT] = PCM
    return attrs


def _flat(attributes: Any) -> str:
    return json.dumps({k: list(v) if isinstance(v, tuple) else v for k, v in dict(attributes or {}).items()})


def _assert_safe(attributes: Any) -> None:
    assert PCM not in _flat(attributes)
    if CONTENT in (attributes or {}):
        assert attributes[CONTENT] == OMITTED


def _ctor_span(provider: Any, filler: int) -> None:
    span = provider.get_tracer("native").start_span("media", attributes=_media(filler))
    span.end()


def _register(exporter: Any, limit: int, batch: bool = False) -> Any:
    return register(project_name="p", span_exporter=exporter, batch=batch, verbose=False,
                    signal_handlers=False, span_limits=SpanLimits(max_span_attributes=limit))


@pytest.mark.parametrize("limit,filler", LIMITS)
@pytest.mark.parametrize("route", ["native", "batch", "init"])
def test_constructor_attributes_evicting_mime_never_export_the_payload(fresh_state, route, limit, filler):
    exporter = InMemorySpanExporter()
    if route == "init":
        provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=[], wire=False,
                                 span_limits=SpanLimits(max_span_attributes=limit))
    else:
        provider = _register(exporter, limit, batch=route == "batch")
    try:
        _ctor_span(provider, filler)
        provider.force_flush()
        (span,) = exporter.get_finished_spans()
    finally:
        if route == "init":
            _manager.shutdown()
        else:
            provider.shutdown()
    assert span.dropped_attributes > 0 and MIME not in span.attributes
    assert span.attributes[CONTENT] == OMITTED
    _assert_safe(span.attributes)
    # Fail closed keeps identity, timing and noncontent metadata.
    assert span.name == "media" and span.attributes[f"meta.{filler - 1:03d}"] == filler - 1


def test_constructor_zero_filler_control_keeps_mime(fresh_state):
    exporter = InMemorySpanExporter()
    provider = _register(exporter, 128)
    _ctor_span(provider, 0)
    provider.shutdown()
    (span,) = exporter.get_finished_spans()
    assert span.attributes[MIME] == "audio/pcm" and span.attributes[CONTENT] == OMITTED


def test_batch_constructor_actual_otlp_never_carries_the_payload(fresh_state, monkeypatch):
    from google.protobuf.json_format import MessageToJson

    collector = _Collector()
    monkeypatch.setenv("FI_BASE_URL", collector.url)
    monkeypatch.setenv("FI_API_KEY", "fi-api-admission-0123456789")
    monkeypatch.setenv("FI_SECRET_KEY", "fi-secret-admission-0123456789")
    try:
        provider = register(project_name="p", batch=True, verbose=False, signal_handlers=False)
        _ctor_span(provider, 140)
        provider.force_flush()
        exported = "".join(MessageToJson(resource) for resource in collector.spans())
        provider.shutdown()
    finally:
        collector.close()
    assert '"media"' in exported and CONTENT in exported
    assert PCM not in exported and OMITTED in exported


@pytest.mark.parametrize("limit,filler", LIMITS)
def test_event_attributes_evicting_mime_never_export_the_payload(fresh_state, limit, filler):
    exporter = InMemorySpanExporter()
    provider = register(project_name="p", span_exporter=exporter, batch=False, verbose=False,
                        signal_handlers=False, span_limits=SpanLimits(max_event_attributes=limit))
    with provider.get_tracer("native").start_as_current_span("media") as span:
        span.add_event("audio", attributes=_media(filler))
        span.add_event("control", attributes=_media(0))
    provider.shutdown()
    (ended,) = exporter.get_finished_spans()
    lossy, control = ended.events
    assert lossy.attributes.dropped > 0 and MIME not in lossy.attributes
    _assert_safe(lossy.attributes)
    assert lossy.attributes[CONTENT] == OMITTED
    assert control.attributes[MIME] == "audio/pcm" and control.attributes[CONTENT] == OMITTED


@pytest.mark.parametrize("how", ["constructor", "add_link"])
def test_link_attributes_evicting_mime_never_export_the_payload(fresh_state, how):
    exporter = InMemorySpanExporter()
    provider = register(project_name="p", span_exporter=exporter, batch=False, verbose=False,
                        signal_handlers=False, span_limits=SpanLimits(max_link_attributes=128))
    tracer = provider.get_tracer("native")
    with tracer.start_as_current_span("peer") as peer:
        context = peer.get_span_context()
    if how == "constructor":
        span = tracer.start_span("media", links=[trace_api.Link(context, _media(140))])
    else:
        span = tracer.start_span("media")
        span.add_link(context, _media(140))
    span.end()
    provider.shutdown()
    ended = [s for s in exporter.get_finished_spans() if s.name == "media"][0]
    (link,) = ended.links
    assert link.attributes.dropped > 0 and MIME not in link.attributes
    _assert_safe(link.attributes)
    assert link.attributes[CONTENT] == OMITTED


@pytest.mark.parametrize("how", ["setter", "constructor"])
def test_host_processor_into_fi_http_exporter_never_carries_the_payload(monkeypatch, how):
    # No FI on_start hook at all: an upstream OTel processor feeds FI's own exporter.
    from google.protobuf.json_format import MessageToJson
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor as OTelSimple

    from fi_instrumentation.otel import HTTPSpanExporter

    collector = _Collector()
    monkeypatch.setenv("FI_BASE_URL", collector.url)
    host = InMemorySpanExporter()
    try:
        provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=128))
        provider.add_span_processor(OTelSimple(HTTPSpanExporter()))
        provider.add_span_processor(OTelSimple(host))
        tracer = provider.get_tracer("native")
        if how == "constructor":
            _ctor_span(provider, 140)
        else:
            with tracer.start_as_current_span("media") as span:
                for key, value in _media(140).items():
                    span.set_attribute(key, value)
        provider.force_flush()
        exported = "".join(MessageToJson(resource) for resource in collector.spans())
        provider.shutdown()
    finally:
        collector.close()
    assert '"media"' in exported and CONTENT in exported
    assert PCM not in exported and OMITTED in exported
    # S8 differential: the application's own exporter keeps its original span.
    (own,) = host.get_finished_spans()
    assert own.attributes[CONTENT] == PCM and own.dropped_attributes == 14


def test_application_view_is_unchanged_with_constructor_attributes(fresh_state):
    # Agent-on vs agent-off: identical live attributes, eviction order and drop counts.
    def application(provider: Any) -> List[Any]:
        span = provider.get_tracer("app").start_span("work", attributes=_media(40))
        span.add_event("e", attributes=_media(140))
        seen = [dict(span.attributes), span._attributes.dropped,
                [dict(e.attributes) for e in span.events], [e.attributes.dropped for e in span.events]]
        span.end()
        return seen

    limits = SpanLimits(max_span_attributes=32)
    plain_host = InMemorySpanExporter()
    plain = TracerProvider(span_limits=limits)
    plain.add_span_processor(SimpleSpanProcessor(plain_host))
    baseline = application(plain)
    plain.shutdown()

    exporter, host = InMemorySpanExporter(), InMemorySpanExporter()
    provider = _register(exporter, 32)
    provider.add_span_processor(SimpleSpanProcessor(host))
    observed = application(provider)
    provider.shutdown()
    assert observed == baseline
    (expected,), (actual,) = plain_host.get_finished_spans(), host.get_finished_spans()
    assert dict(actual.attributes) == dict(expected.attributes)
    assert actual.dropped_attributes == expected.dropped_attributes
    assert dict(actual.events[0].attributes) == dict(expected.events[0].attributes)
    (fi,) = exporter.get_finished_spans()
    _assert_safe(fi.attributes)
    _assert_safe(fi.events[0].attributes)


def test_recorded_evictions_do_not_fail_closed_and_resanitizing_is_stable():
    # A live, adopted container whose evictions were all recorded is sanitized precisely (text
    # survives), and its sanitized snapshot (a plain SDK container with a nonzero drop count) is
    # stamped accounted, so a second boundary does not mistake it for an unrecorded loss.
    from opentelemetry.attributes import BoundedAttributes

    from fi_instrumentation.instrumentation._spans import FiSpan

    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=16))
    raw = provider.get_tracer("lib").start_span("chat")
    span = FiSpan(raw, TraceConfig())
    for i in range(20):
        span.set_attribute(f"meta.{i:03d}", i)
    span.set_attribute("input.value", "hello there")
    span.set_attribute("output.value", "general kenobi")
    span.end()
    provider.shutdown()
    assert raw._attributes.dropped > 0 and not _limits.unaccounted_loss(raw._attributes)
    clean = export_span(raw, TraceConfig())
    assert clean.attributes["input.value"] == "hello there"
    assert clean.attributes["output.value"] == "general kenobi"
    assert clean.dropped_attributes == raw.dropped_attributes > 0
    assert type(clean._attributes) is BoundedAttributes
    assert not _limits.unaccounted_loss(clean._attributes)  # the snapshot stamp
    again = export_span(clean, TraceConfig())
    assert again.attributes["input.value"] == "hello there"
    assert again.dropped_attributes == raw.dropped_attributes
    # Without the stamp the same plain container would be an unrecorded loss.
    unstamped = BoundedAttributes(maxlen=None)
    unstamped.dropped = clean._attributes.dropped
    assert _limits.unaccounted_loss(unstamped)


def test_unrecorded_loss_fail_closed_keeps_metadata_and_counts():
    # The sanitizer-level rule on a plain SDK container with unrecorded drops.
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=8))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    attrs = {MIME: "audio/pcm", "gen_ai.request.model": "m", "input.value": "plain words",
             "gen_ai.audio.transcript": "spoken words", "gen_ai.usage.input_tokens": 3,
             "meta.a": 1, "meta.b": 2, "meta.c": 3, "meta.d": 4, CONTENT: PCM}
    provider.get_tracer("native").start_span("media", attributes=attrs).end()
    provider.shutdown()
    (raw,) = exporter.get_finished_spans()
    assert raw._attributes.dropped == 2 and MIME not in raw.attributes
    assert _limits.unaccounted_loss(raw._attributes)
    clean = export_span(raw, TraceConfig())
    assert clean.attributes[CONTENT] == OMITTED
    assert clean.attributes["input.value"] == "<omitted: media>"  # content dropped, fail closed
    assert clean.attributes["gen_ai.audio.transcript"] == "spoken words"  # metadata boundary
    assert clean.attributes["gen_ai.usage.input_tokens"] == 3
    assert clean.dropped_attributes == 2
    assert not _limits.unaccounted_loss(clean._attributes)


def test_unrecorded_loss_survives_adoption_and_a7_projection():
    # A span whose constructor already evicted (no hook yet) is adopted by FiSpan, written to
    # past the limit and projected at end: the loss stays unaccounted through every container.
    from fi_instrumentation.instrumentation._spans import FiSpan

    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=16))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    raw = provider.get_tracer("native").start_span("media", attributes=_media(20))
    assert raw._attributes.dropped == 6 and MIME not in raw.attributes
    span = FiSpan(raw, TraceConfig())
    assert isinstance(raw._attributes, _limits.AdmissionAttributes)
    assert _limits.unaccounted_loss(raw._attributes)
    for i in range(20):
        span.set_attribute(f"late.{i:03d}", i)
    span.set_attribute("gen_ai.request.model", "m")
    span.set_attribute("input.parts.1.content", PCM)
    span.end()
    provider.shutdown()
    # The live span now holds the A7-projected container; it still carries the loss.
    assert isinstance(raw._attributes, _limits.AdmissionAttributes)
    assert _limits.unaccounted_loss(raw._attributes)
    clean = export_span(raw, TraceConfig())
    _assert_safe(clean.attributes)
    assert clean.attributes.get("input.parts.1.content") == OMITTED
    assert clean.attributes["gen_ai.request.model"] == "m"
    # The standalone FITracer snapshot route exported an already-sanitized, accounted copy.
    (ended,) = exporter.get_finished_spans()
    _assert_safe(ended.attributes)
    assert not _limits.unaccounted_loss(ended._attributes)
