"""Round 6 pass A (r6a verifier #1): media context survives SDK admission and A7 projection.

Destructive SDK admission (oldest-first eviction under the attribute count limit) and the A7
projection used to run before the S1 sanitizer saw the attributes, so either could drop a MIME
sibling while keeping its short PCM payload; the payload then exported raw. Admission now
records media context (bounded) on the SDK container, the one sanitizer reads it, and the
projection keeps a MIME/type companion in the same atomic group as its payload.

Synthetic payloads and loopback collectors only.
"""

import json
from typing import Any, Dict, List

import pytest
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer, TraceConfig, register
from fi_instrumentation import _attribute_limits as _limits
from fi_instrumentation._attribute_limits import DROPPED_CONTENT_COUNT, project_attributes
from fi_instrumentation.auto import _manager
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_round6a import PROTECTED, _Collector

PCM = "AQIDBA=="  # 4 raw PCM bytes: no magic, recognisable only through its MIME sibling
OMITTED = "<omitted: 8 base64 chars>"
MIME = "input.parts.0.mime_type"
CONTENT = "input.parts.0.content"


def _media_span(tracer: Any, filler: int, *, protected: int = 0, raw: bool = False) -> None:
    """MIME first, ``filler`` metadata attributes, then the payload (the SDK evicts oldest first)."""
    with tracer.start_as_current_span("media") as span:
        target = getattr(span, "__wrapped__", span) if raw else span
        for key, value in list(PROTECTED.items())[:protected]:
            span.set_attribute(key, value)
        target.set_attribute(MIME, "audio/pcm")
        for i in range(filler):
            target.set_attribute(f"meta.{i:03d}", i)
        target.set_attribute(CONTENT, PCM)


def _fi_tracer(provider: Any) -> FITracer:
    return FITracer(provider.get_tracer("library"), config=TraceConfig())


def _flat(attributes: Any) -> str:
    return json.dumps({k: list(v) if isinstance(v, tuple) else v for k, v in dict(attributes).items()})


def _assert_media_safe(attributes: Any) -> None:
    assert PCM not in _flat(attributes)
    if CONTENT in attributes:
        assert attributes[CONTENT] == OMITTED


@pytest.mark.parametrize("limit,filler", [(128, 140), (32, 40), (8, 20)])
def test_manual_register_sdk_eviction_keeps_media_context(fresh_state, limit, filler):
    exporter = InMemorySpanExporter()
    provider = register(project_name="p", span_exporter=exporter, batch=False, verbose=False,
                        signal_handlers=False, span_limits=SpanLimits(max_span_attributes=limit))
    _media_span(_fi_tracer(provider), filler)
    provider.shutdown()
    (span,) = exporter.get_finished_spans()
    assert span.dropped_attributes > 0
    assert MIME not in span.attributes  # evicted by the SDK; its media context is not lost
    assert span.attributes.get(CONTENT) == OMITTED
    _assert_media_safe(span.attributes)


def test_zero_filler_control_keeps_mime_and_omits_payload(fresh_state):
    exporter = InMemorySpanExporter()
    provider = register(project_name="p", span_exporter=exporter, batch=False, verbose=False,
                        signal_handlers=False)
    _media_span(_fi_tracer(provider), 0)
    provider.shutdown()
    (span,) = exporter.get_finished_spans()
    assert span.attributes[MIME] == "audio/pcm" and span.attributes[CONTENT] == OMITTED


def test_manual_register_actual_otlp_payload_is_omitted(fresh_state, monkeypatch):
    from google.protobuf.json_format import MessageToJson

    collector = _Collector()
    monkeypatch.setenv("FI_BASE_URL", collector.url)
    monkeypatch.setenv("FI_API_KEY", "fi-api-media-0123456789")
    monkeypatch.setenv("FI_SECRET_KEY", "fi-secret-media-0123456789")
    try:
        provider = register(project_name="p", batch=False, verbose=False, signal_handlers=False)
        _media_span(_fi_tracer(provider), 140)
        provider.force_flush()
        exported = "".join(MessageToJson(resource) for resource in collector.spans())
        provider.shutdown()
    finally:
        collector.close()
    assert '"media"' in exported and CONTENT in exported
    assert PCM not in exported and OMITTED in exported


def test_one_line_init_sdk_eviction_keeps_media_context(fresh_state):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=[], wire=False,
                             span_limits=SpanLimits(max_span_attributes=128))
    try:
        _media_span(_fi_tracer(provider), 140)
        provider.force_flush()
        (span,) = exporter.get_finished_spans()
    finally:
        _manager.shutdown()
    assert MIME not in span.attributes
    _assert_media_safe(span.attributes)
    assert span.attributes.get(CONTENT) == OMITTED


@pytest.mark.parametrize("limit", [128, 32, 8])
def test_projection_alone_never_strands_a_payload_without_its_mime(limit):
    # No SDK drop: the reservoir's protected keys push the candidate set over the limit, and
    # output content leaves exactly two slots for input content on the first pass, so the
    # count-marker recompute leaves one: the lexically first payload used to win alone.
    protected = min(len(PROTECTED), limit - 4)
    outputs = limit - protected - 2
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=limit))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = _fi_tracer(provider)
    with tracer.start_as_current_span("media") as span:
        for key, value in list(PROTECTED.items())[:protected]:
            span.set_attribute(key, value)
        span.set_attribute(MIME, "audio/pcm")
        span.set_attribute(CONTENT, PCM)
        span.set_attribute("input.zzz", "later input")
        for i in range(outputs):
            span.set_attribute(f"gen_ai.output.messages.{i}.message.content", "o")
        assert span.__wrapped__._attributes.dropped == 0  # projection is the only stage
    provider.shutdown()
    (ended,) = exporter.get_finished_spans()
    assert ended.attributes[DROPPED_CONTENT_COUNT] >= 1
    _assert_media_safe(ended.attributes)
    assert (MIME in ended.attributes) == (CONTENT in ended.attributes)


def test_projection_groups_mime_and_type_companions_with_their_payload():
    for companion, value in ((MIME, "audio/pcm"), ("input.parts.0.type", "input_audio")):
        attrs = {**PROTECTED, companion: value, CONTENT: PCM, "input.zzz": "z"}
        room = len(PROTECTED) + 2  # two content slots, then one is taken by the count marker
        selected, _ = project_attributes(attrs, room, "chat")
        assert companion not in selected and CONTENT not in selected
        assert selected["input.zzz"] == "z" and selected[DROPPED_CONTENT_COUNT] == 2
        selected, _ = project_attributes(attrs, room + 1, "chat")
        assert selected[companion] == value and selected[CONTENT] == PCM
    # A non-media "type" is not a companion: unrelated parts stay independently sized.
    attrs = {**PROTECTED, "input.parts.0.type": "text", "input.parts.0.text": "hello", "input.zzz": "z"}
    selected, _ = project_attributes(attrs, len(PROTECTED) + 2, "chat")
    assert [k for k in selected if k.startswith("input.")] == ["input.parts.0.text"]


def test_projection_grouping_is_deterministic_under_insertion_order():
    import random

    attrs = {**PROTECTED, MIME: "audio/pcm", CONTENT: PCM, "input.parts.1.mime_type": "image/png",
             "input.parts.1.data": "x", "input.extra": "y", "output.value": "o"}
    items = list(attrs.items())
    expected = project_attributes(dict(items), len(PROTECTED) + 4, "chat")
    for seed in range(6):
        random.Random(seed).shuffle(items)
        assert project_attributes(dict(items), len(PROTECTED) + 4, "chat") == expected


@pytest.mark.parametrize("raw", [False, True])
def test_native_and_raw_sdk_writes_on_owned_route_keep_media_context(fresh_state, raw):
    # Library code writing straight to the SDK span, and SDK-native spans on a register() route.
    exporter = InMemorySpanExporter()
    host = InMemorySpanExporter()
    provider = register(project_name="p", span_exporter=exporter, batch=False, verbose=False,
                        signal_handlers=False, span_limits=SpanLimits(max_span_attributes=32))
    provider.add_span_processor(SimpleSpanProcessor(host))  # application-owned destination
    tracer = _fi_tracer(provider) if raw else provider.get_tracer("native")
    _media_span(tracer, 40, raw=raw)
    provider.shutdown()
    (span,) = exporter.get_finished_spans()
    assert MIME not in span.attributes
    assert span.attributes.get(CONTENT) == OMITTED
    if not raw:
        # S8: a host-owned exporter keeps the application's own (unsanitized) view.
        assert host.get_finished_spans()[0].attributes[CONTENT] == PCM


def test_agent_protected_provider_native_span(fresh_state):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=[], wire=False,
                             span_limits=SpanLimits(max_span_attributes=32))
    try:
        _media_span(provider.get_tracer("native"), 40)
        provider.force_flush()
        (span,) = exporter.get_finished_spans()
    finally:
        _manager.shutdown()
    assert span.attributes.get(CONTENT) == OMITTED


def test_later_non_media_mime_clears_the_admitted_context():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=16))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with _fi_tracer(provider).start_as_current_span("text") as span:
        span.set_attribute(MIME, "audio/pcm")
        span.set_attribute(MIME, "text/plain")  # the latest admitted value decides
        for i in range(20):
            span.set_attribute(f"meta.{i:03d}", i)
        span.set_attribute(CONTENT, PCM)
    provider.shutdown()
    (ended,) = exporter.get_finished_spans()
    assert ended.attributes[CONTENT] == PCM


def test_admission_hint_overflow_fails_closed():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=16))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with _fi_tracer(provider).start_as_current_span("many") as span:
        for i in range(_limits.MAX_ADMISSION_HINTS + 64):
            span.set_attribute(f"input.parts.{i}.mime_type", "audio/pcm")
        span.set_attribute(CONTENT, PCM)
        span.set_attribute("gen_ai.request.model", "m")
    provider.shutdown()
    (ended,) = exporter.get_finished_spans()
    assert ended.name == "span" and dict(ended.attributes) == {}  # identity/timing only


def test_differential_application_view_is_unchanged(fresh_state):
    # Agent-on vs agent-off: the application's own spans keep the same attributes, eviction
    # order and dropped count; only the traceAI destination is sanitized.
    def application(provider: Any) -> Dict[str, Any]:
        seen: List[Any] = []
        tracer = provider.get_tracer("app")
        with tracer.start_as_current_span("work") as span:
            span.set_attribute(MIME, "audio/pcm")
            for i in range(40):
                span.set_attribute(f"meta.{i:03d}", i)
            span.set_attribute(CONTENT, PCM)
            seen.append(dict(span.attributes))
            seen.append(span._attributes.dropped)
        return {"live": seen}

    limits = SpanLimits(max_span_attributes=32)
    plain_host = InMemorySpanExporter()
    plain = TracerProvider(span_limits=limits)
    plain.add_span_processor(SimpleSpanProcessor(plain_host))
    baseline = application(plain)
    plain.shutdown()

    exporter = InMemorySpanExporter()
    host = InMemorySpanExporter()
    provider = register(project_name="p", span_exporter=exporter, batch=False, verbose=False,
                        signal_handlers=False, span_limits=limits)
    provider.add_span_processor(SimpleSpanProcessor(host))
    observed = application(provider)
    provider.shutdown()
    assert observed == baseline
    (expected,), (actual,) = plain_host.get_finished_spans(), host.get_finished_spans()
    assert dict(actual.attributes) == dict(expected.attributes)
    assert actual.dropped_attributes == expected.dropped_attributes
    assert exporter.get_finished_spans()[0].attributes[CONTENT] == OMITTED


def test_older_sdk_inline_admission_path_records_evicted_media_context(monkeypatch):
    # OpenTelemetry 1.29-1.39 admit and evict inside __setitem__ (no _setitem_locked).
    import importlib.util
    import threading
    from collections import OrderedDict
    from collections.abc import MutableMapping

    import opentelemetry.attributes as otel_attributes

    class OldBounded(MutableMapping):
        def __init__(self, maxlen=None, attributes=None, immutable=True, max_value_len=None,
                     extended_attributes=False):
            self.maxlen, self.dropped, self.max_value_len = maxlen, 0, max_value_len
            self._dict: Any = {}
            self._lock = threading.RLock()
            if attributes:
                for key, value in attributes.items():
                    self[key] = value
            self._immutable = immutable

        def __setitem__(self, key, value):
            if getattr(self, "_immutable", False):
                raise TypeError
            with self._lock:
                if self.maxlen is not None and self.maxlen == 0:
                    self.dropped += 1
                    return
                if key in self._dict:
                    del self._dict[key]
                elif self.maxlen is not None and len(self._dict) == self.maxlen:
                    if not isinstance(self._dict, OrderedDict):
                        self._dict = OrderedDict(self._dict)
                    self._dict.popitem(last=False)
                    self.dropped += 1
                self._dict[key] = value

        def __getitem__(self, key):
            return self._dict[key]

        def __delitem__(self, key):
            del self._dict[key]

        def __iter__(self):
            return iter(self._dict.copy())

        def __len__(self):
            return len(self._dict)

    monkeypatch.setattr(otel_attributes, "BoundedAttributes", OldBounded)
    spec = importlib.util.spec_from_file_location("_old_sdk_attribute_limits", _limits.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "__setitem__" in vars(module.AdmissionAttributes)
    assert "_setitem_locked" not in vars(module.AdmissionAttributes)

    plain = OldBounded(maxlen=4, immutable=False)
    recording = module.AdmissionAttributes.adopt(OldBounded(maxlen=4, immutable=False))
    for target in (plain, recording):
        target[MIME] = "audio/pcm"
        for i in range(6):
            target[f"meta.{i}"] = i
        target[CONTENT] = PCM
    assert list(recording) == list(plain) and recording.dropped == plain.dropped
    assert MIME not in recording
    assert module.admission_hints(recording) == {"input.parts.0": (True, True)}
    recording[MIME] = "text/plain"  # re-admitted: its present value decides again
    assert module.admission_hints(recording) is None
    projected = module.AdmissionAttributes(maxlen=2, attributes={CONTENT: PCM}, immutable=False)
    assert dict(projected) == {CONTENT: PCM}
