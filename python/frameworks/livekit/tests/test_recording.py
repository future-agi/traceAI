"""Recording contracts: fake services, local providers, no RPC or media access."""

import asyncio
import builtins
import importlib
from pathlib import Path
from types import SimpleNamespace as Info

import pytest
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, Sampler

import traceai_livekit
from traceai_livekit.exporters.base_exporter import BaseMappedSpanExporter


METHODS = (
    "start_room_composite_egress", "start_web_egress", "start_participant_egress",
    "start_track_composite_egress", "start_track_egress", "stop_egress",
    "update_stream", "update_layout",
)


def _apis():
    # Intentional RED assertions on the base, not collection errors or optional skips.
    record = getattr(traceai_livekit, "record_egress", None)
    wrap = getattr(traceai_livekit, "wrap_egress_client", None)
    assert callable(record), "public record_egress is missing"
    assert callable(wrap), "public wrap_egress_client is missing"
    return record, wrap


class MappedMemory(BaseMappedSpanExporter, InMemorySpanExporter):
    pass


class ObserveSampler(Sampler):
    def __init__(self):
        self.attributes = []

    def should_sample(self, parent_context, trace_id, name, kind=None,
                      attributes=None, links=None, trace_state=None):
        self.attributes.append(dict(attributes or {}))
        return ALWAYS_ON.should_sample(parent_context, trace_id, name, kind,
                                       attributes, links, trace_state)

    def get_description(self):
        return "recording-test"


class OnStart(SpanProcessor):
    def __init__(self):
        self.attributes = []

    def on_start(self, span, parent_context=None):
        self.attributes.append(dict(span.attributes or {}))

    def on_end(self, span):
        pass

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


@pytest.fixture
def harness():
    sampler = ObserveSampler()
    provider = TracerProvider(sampler=sampler)
    mapped, raw, starts = MappedMemory(), InMemorySpanExporter(), OnStart()
    provider.add_span_processor(starts)
    provider.add_span_processor(SimpleSpanProcessor(mapped))
    provider.add_span_processor(SimpleSpanProcessor(raw))
    tracer = provider.get_tracer("recording-test")
    root = tracer.start_span("session-envelope")
    yield Info(provider=provider, tracer=tracer, root=root, mapped=mapped,
               raw=raw, starts=starts, sampler=sampler)
    if root.is_recording():
        root.end()
    provider.shutdown()


def _info(**changes):
    fields = dict(egress_id="egress-synthetic", room_name="room-synthetic", status=0,
                  started_at=1700000000000000001, ended_at=0,
                  file_results=[], stream_results=[])
    fields.update(changes)
    return Info(**fields)


def _recordings(exporter):
    return [s for s in exporter.get_finished_spans() if s.name.startswith("livekit.recording.")]


def _capture(h, info=None, **kwargs):
    record, _ = _apis()
    options = dict(session_span=h.root, tracer=h.tracer)
    options.update(kwargs)
    return record(info if info is not None else _info(), **options)


def test_public_recording_apis_are_exported_without_optional_livekit_imports(monkeypatch):
    original = builtins.__import__

    def no_livekit(name, *args, **kwargs):
        if name == "livekit" or name.startswith("livekit."):
            raise AssertionError("optional runtime imported")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_livekit)
    importlib.reload(traceai_livekit)
    record, wrap = _apis()
    assert {"record_egress", "wrap_egress_client"}.issubset(traceai_livekit.__all__)
    assert callable(record) and callable(wrap)
    # Force a fresh import of the implementation too, not just cached public exports.
    importlib.reload(importlib.import_module("traceai_livekit.recording"))


def test_record_egress_creates_a_session_child_with_a_complete_metadata_snapshot(harness):
    h = harness
    root_before = dict(h.root.attributes)
    response = _info(status="EGRESS_COMPLETE", ended_at=1700000001000000001,
                     file_results=[Info(location="s3://test-bucket/call.mp4")])
    assert _capture(h, response, conversation_id="conversation-synthetic") is True
    (s,) = _recordings(h.mapped)
    assert s.context.trace_id == h.root.get_span_context().trace_id
    assert s.parent.span_id == h.root.get_span_context().span_id
    assert s.kind is trace.SpanKind.INTERNAL
    assert s.attributes[SpanAttributes.GEN_AI_SPAN_KIND] == "CHAIN"
    assert s.attributes["livekit.recording.egress_id"] == response.egress_id
    assert s.attributes["livekit.recording.status"] == "EGRESS_COMPLETE"
    assert s.attributes["livekit.recording.started_at_unix_nano"] == response.started_at
    assert s.attributes["livekit.recording.ended_at_unix_nano"] == response.ended_at
    assert s.attributes["livekit.recording.file_locations"] == ("s3://test-bucket/call.mp4",)
    assert s.attributes["livekit.recording.room_name"] == "room-synthetic"
    assert s.attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == "conversation-synthetic"
    assert s.end_time >= s.start_time > response.ended_at
    assert dict(h.root.attributes) == root_before
    assert h.root.status.status_code is trace.StatusCode.UNSET
    (raw,) = _recordings(h.raw)
    assert not any(k.startswith("livekit.recording.") for k in raw.attributes)
    assert SpanAttributes.GEN_AI_CONVERSATION_ID not in raw.attributes


def test_saved_ended_parent_beats_unrelated_current_context(harness):
    h = harness
    saved = trace.NonRecordingSpan(h.root.get_span_context())
    h.root.end()
    with h.tracer.start_as_current_span("unrelated") as other:
        assert _capture(h, session_span=saved)
        (s,) = _recordings(h.raw)
        assert s.context.trace_id == saved.get_span_context().trace_id
        assert s.context.trace_id != other.get_span_context().trace_id
        assert s.parent.span_id == saved.get_span_context().span_id


def test_current_parent_and_default_conversation(harness):
    record, _ = _apis()
    with trace.use_span(harness.root, end_on_exit=False):
        assert record(_info(), tracer=harness.tracer, operation="untrusted-operation")
    (s,) = _recordings(harness.mapped)
    assert s.name == "livekit.recording.snapshot"
    assert s.attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == "room-synthetic"


def test_no_valid_parent_or_suppressed_recording_returns_false_without_orphan_spans(harness):
    record, wrap = _apis()
    h = harness
    token = context.attach(context.Context())
    try:
        assert record(_info(), tracer=h.tracer) is False
        service = FakeService(_info())
        unbound = wrap(service, tracer=h.tracer)
    finally:
        context.detach(token)
    with trace.use_span(h.root, end_on_exit=False):
        assert record(_info(), session_span=trace.INVALID_SPAN, tracer=h.tracer) is False
        assert asyncio.run(unbound.stop_egress(object())) is service.response
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert _capture(h) is False
    finally:
        context.detach(token)
    dropped = TracerProvider(sampler=ALWAYS_OFF)
    try:
        assert _capture(h, tracer=dropped.get_tracer("off")) is False
    finally:
        dropped.shutdown()
    assert _recordings(h.raw) == []


class FakeService:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.response

    start_room_composite_egress = call
    start_web_egress = call
    start_participant_egress = call
    start_track_composite_egress = call
    start_track_egress = call
    stop_egress = call
    update_stream = call
    update_layout = call
    list_egress = call


@pytest.mark.parametrize("method", METHODS)
def test_wrapped_start_stop_and_update_capture_once_without_changing_the_call(harness, method):
    _, wrap = _apis()
    h = harness
    response, request, option = _info(), object(), object()
    service = FakeService(response)
    wrapped = wrap(service, session_span=h.root, room_name="room-synthetic", tracer=h.tracer)
    method_call = getattr(wrapped, method)
    assert getattr(wrapped, method) is method_call
    assert asyncio.run(method_call(request, option=option)) is response
    assert len(service.calls) == 1
    assert service.calls[0][0][0] is request
    assert service.calls[0][1]["option"] is option
    (s,) = _recordings(h.raw)
    assert s.name == "livekit.recording." + method
    assert s.parent.span_id == h.root.get_span_context().span_id


def test_multiple_recordings_and_concurrent_sessions_cannot_cross_bind(harness):
    _, wrap = _apis()
    h = harness
    second = h.tracer.start_span("second-session")
    a, b = FakeService(_info(egress_id="egress-a")), FakeService(_info(egress_id="egress-b"))
    wa = wrap(a, session_span=h.root, conversation_id="a", tracer=h.tracer)
    wb = wrap(b, session_span=second, conversation_id="b", tracer=h.tracer)
    assert wrap(wa) is wa
    assert wrap(wa, session_span=h.root, conversation_id="a", tracer=h.tracer) is wa
    with pytest.raises(ValueError, match="binding"):
        wrap(wa, session_span=second)

    async def run():
        await asyncio.gather(wa.start_room_composite_egress(object()), wb.stop_egress(object()))
        a.response = _info(egress_id="egress-a2")
        await wa.start_room_composite_egress(object())

    try:
        with h.tracer.start_as_current_span("unrelated"):
            asyncio.run(run())
        spans = _recordings(h.mapped)
        assert len(spans) == 3
        for s in spans:
            is_b = s.attributes["livekit.recording.egress_id"] == "egress-b"
            parent = second if is_b else h.root
            assert s.parent.span_id == parent.get_span_context().span_id
            assert s.context.trace_id == parent.get_span_context().trace_id
            assert s.attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == ("b" if is_b else "a")
    finally:
        second.end()


class EightOnly:
    def __iter__(self):
        for _ in range(8):
            yield Info(location="safe/call.mp4")
        raise AssertionError("ninth result was examined")


class TrapInfo:
    egress_id = "egress-trap"
    status = 0
    ended_at = 0
    started_at = -1
    file_results = EightOnly()
    stream_results = []
    room_name = "room-synthetic"

    @property
    def request(self):
        raise AssertionError("request read")

    def SerializeToString(self):
        raise AssertionError("protobuf serialized")

    def __str__(self):
        raise AssertionError("response stringified")


def test_partial_and_bounded_response_extraction_never_serializes_requests_or_media(harness):
    h = harness
    assert _capture(h, TrapInfo())
    (s,) = _recordings(h.mapped)
    assert s.attributes["livekit.recording.status"] == "0"
    assert "livekit.recording.started_at_unix_nano" not in s.attributes
    assert "livekit.recording.ended_at_unix_nano" not in s.attributes
    assert len(s.attributes["livekit.recording.file_locations"]) <= 8
    assert _capture(h, _info(egress_id="")) is False
    assert _capture(h, _info(egress_id="x" * 257)) is False
    assert _capture(h, _info(started_at=True, ended_at=2**63, status=True,
                             file_results=[Info(location="x" * 2049)]))
    last = _recordings(h.mapped)[-1].attributes
    for name in ("status", "started_at_unix_nano", "ended_at_unix_nano", "file_locations"):
        assert "livekit.recording." + name not in last


@pytest.mark.parametrize("location,expected", [
    ("https://synthetic-user:synthetic-password@storage.example/call.mp4?X-Amz-Signature=SENTINEL#SENTINEL", "https://storage.example/call.mp4"),
    ("https://storage.example/call.mp4?X-Goog-Signature=SENTINEL&sig=SENTINEL", "https://storage.example/call.mp4"),
    ("https://storage.example/call.mp4?sig=SENTINEL&SIG=SENTINEL&%73ig=SENTINEL", "https://storage.example/call.mp4"),
    ("recordings/call.mp4", "recordings/call.mp4"),
    ("../private.mp4", None), ("%2e%2e/private.mp4", None),
    ("/private.mp4", None), ("C:/private.mp4", None),
    ("https://host\\SENTINEL/path", None), ("https://host/\nSENTINEL", None),
    ("https://[broken/SENTINEL", None), ("https://host:bad/SENTINEL", None),
    ("javascript:SENTINEL", None), ("//host/SENTINEL", None),
    ("https://host/%0aSENTINEL", None),
    ("https://[fe80::1%SENTINEL]/call.mp4", None),
    ("https://[2001:db8::1]:443/call.mp4?sig=SENTINEL", "https://[2001:db8::1]:443/call.mp4"),
])
def test_presigned_and_stream_credentials_never_reach_any_span_observer(harness, caplog, location, expected):
    h = harness
    response = _info(file_results=[Info(location=location, filename="do-not-fallback.mp4")],
                     stream_results=[Info(url="rtmps://user:SENTINEL@stream.example:443/live/SENTINEL?token=SENTINEL#SENTINEL")])
    assert _capture(h, response)
    s = _recordings(h.mapped)[0]
    if expected is None:
        assert "livekit.recording.file_locations" not in s.attributes
    else:
        assert s.attributes["livekit.recording.file_locations"] == (expected,)
    assert s.attributes["livekit.recording.stream_locations"] == ("rtmps://stream.example:443",)
    observed = [h.sampler.attributes, h.starts.attributes,
                [dict(x.attributes) for x in h.raw.get_finished_spans()],
                [dict(x.attributes) for x in h.mapped.get_finished_spans()], caplog.text]
    for value in observed:
        assert "SENTINEL" not in str(value)
        assert "synthetic-password" not in str(value)
    assert not s.events
    assert s.status.description is None


class WithholdConversation(TraceConfig):
    def mask(self, key, value):
        if key == SpanAttributes.GEN_AI_CONVERSATION_ID:
            return None
        return super().mask(key, value)


class WithholdLocations(TraceConfig):
    def mask(self, key, value):
        if key == SpanAttributes.OUTPUT_VALUE:
            return None
        return super().mask(key, value)


class SpyConfig(TraceConfig):
    calls = None

    def mask(self, key, value):
        self.calls.append(key)
        return super().mask(key, value)


def test_existing_fi_masks_remain_authoritative_for_every_new_value(harness):
    h = harness
    response = _info(file_results=[Info(location="https://storage.example/call.mp4?sig=SENTINEL")])
    assert _capture(h, response, trace_config=WithholdConversation())
    assert SpanAttributes.GEN_AI_CONVERSATION_ID not in _recordings(h.mapped)[-1].attributes
    strong = FITracer(h.tracer, config=WithholdLocations())
    assert _capture(h, response, tracer=strong)
    assert "livekit.recording.file_locations" not in _recordings(h.mapped)[-1].attributes
    assert "lk.pii.recording.file_locations" not in _recordings(h.raw)[-1].attributes
    calls = []
    policy = SpyConfig()
    object.__setattr__(policy, "calls", calls)
    assert _capture(h, response, trace_config=policy)
    attrs = _recordings(h.mapped)[-1].attributes
    for key in attrs:
        if key.startswith(("livekit.recording.", "lk.recording.", "lk.pii.")) or key == SpanAttributes.GEN_AI_CONVERSATION_ID:
            assert key in calls
    assert SpanAttributes.OUTPUT_VALUE in calls


def test_builtin_output_hiding_also_gates_locations(harness):
    assert _capture(harness, _info(file_results=[Info(location="safe/call.mp4")]),
                    trace_config=TraceConfig(hide_outputs=True))
    for s in _recordings(harness.raw) + _recordings(harness.mapped):
        assert "safe/call.mp4" not in str(dict(s.attributes))


@pytest.mark.parametrize("error", [RuntimeError("SENTINEL"), asyncio.CancelledError("SENTINEL")])
def test_delegate_errors_cancellation_and_capture_failures_do_not_change_api_behavior(harness, caplog, error):
    _, wrap = _apis()
    h = harness
    service = FakeService(error=error)
    proxy = wrap(service, session_span=h.root, tracer=h.tracer)
    with pytest.raises(type(error)) as caught:
        asyncio.run(proxy.stop_egress(object()))
    assert caught.value is error
    assert len(service.calls) == 1
    assert _recordings(h.raw) == []

    class BrokenInfo:
        @property
        def egress_id(self):
            raise RuntimeError("SENTINEL")

    service.error, service.response = None, BrokenInfo()
    assert asyncio.run(proxy.stop_egress(object())) is service.response
    assert _recordings(h.raw) == []
    assert "SENTINEL" not in caplog.text


def test_masking_failure_is_atomic_and_baseexceptions_are_not_swallowed(harness, caplog):
    class Broken(TraceConfig):
        def mask(self, key, value):
            raise RuntimeError("SENTINEL")

    assert _capture(harness, trace_config=Broken()) is False
    assert _recordings(harness.raw) == []
    assert "SENTINEL" not in caplog.text

    class Interrupted(TraceConfig):
        def mask(self, key, value):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        _capture(harness, trace_config=Interrupted())


def test_unknown_methods_and_non_info_returns_stay_pass_through(harness, caplog):
    _, wrap = _apis()
    h = harness
    service = FakeService(None)
    proxy = wrap(service, session_span=h.root, room_name="expected", tracer=h.tracer)
    assert asyncio.run(proxy.list_egress(object())) is None
    assert asyncio.run(proxy.stop_egress(object())) is None
    service.response = _info(room_name="other-room")
    assert asyncio.run(proxy.stop_egress(object())) is service.response
    service.start_web_egress = lambda request: request
    assert proxy.start_web_egress is service.start_web_egress
    sentinel = object()
    assert proxy.start_web_egress(sentinel) is sentinel
    with pytest.raises(AttributeError):
        proxy.not_a_method
    assert _recordings(h.raw) == []
    assert "expected" not in caplog.text and "other-room" not in caplog.text


def test_missing_optional_fields_are_not_invented(harness):
    assert _capture(harness, Info(egress_id="only-id"))
    attrs = _recordings(harness.mapped)[0].attributes
    assert "livekit.recording.status" not in attrs
    assert SpanAttributes.GEN_AI_CONVERSATION_ID not in attrs
    assert "livekit.recording.room_name" not in attrs


def test_oversize_response_room_cannot_bypass_explicit_binding(harness):
    assert _capture(harness, _info(room_name="x" * 257), room_name="expected") is False
    assert _recordings(harness.raw) == []


def test_withheld_or_invalid_recording_marker_cannot_resurrect_conversation(harness):
    class DropMarker(WithholdConversation):
        def mask(self, key, value):
            if key == "livekit.recording.operation":
                return None
            return super().mask(key, value)

    assert _capture(harness, trace_config=DropMarker()) is False
    assert _recordings(harness.raw) == []


def test_recording_mapping_drops_stale_aliases_without_retained_sources():
    from traceai_livekit.exporters.base_exporter import _map_attributes_to_fi_conventions

    attrs = _map_attributes_to_fi_conventions({
        "lk.recording.operation": "snapshot",
        "lk.pii.room_name": "room-synthetic",
        SpanAttributes.GEN_AI_CONVERSATION_ID: "stale-conversation",
        "livekit.recording.file_locations": ("stale.mp4",),
    })
    assert SpanAttributes.GEN_AI_CONVERSATION_ID not in attrs
    assert "livekit.recording.file_locations" not in attrs


def test_redacted_alias_is_not_replaced_with_raw_room_or_filename(harness):
    class Redact(TraceConfig):
        def mask(self, key, value):
            if key in (SpanAttributes.GEN_AI_CONVERSATION_ID,
                       "livekit.recording.file_locations"):
                return "REDACTED"
            return super().mask(key, value)

    assert _capture(harness, _info(file_results=[Info(location="safe/call.mp4", filename="raw.mp4")]),
                    trace_config=Redact())
    attrs = _recordings(harness.mapped)[0].attributes
    assert attrs[SpanAttributes.GEN_AI_CONVERSATION_ID] == "REDACTED"
    assert attrs["livekit.recording.file_locations"] == ("REDACTED",)
    for s in _recordings(harness.raw) + _recordings(harness.mapped):
        assert "safe/call.mp4" not in str(dict(s.attributes))
        assert "raw.mp4" not in str(dict(s.attributes))


def test_stream_origin_and_safe_filename_fallback(harness):
    assert _capture(harness, _info(
        file_results=[Info(location="", filename="recordings/call.mp4"),
                      Info(location=None, filename="/absolute.mp4")],
        stream_results=[Info(url="rtmp://stream.example/SENTINEL")],
    ))
    attrs = _recordings(harness.mapped)[0].attributes
    assert attrs["livekit.recording.file_locations"] == ("recordings/call.mp4",)
    assert attrs["livekit.recording.stream_locations"] == ("rtmp://stream.example",)


def test_egress_example_and_docs_demonstrate_supported_trace_linkage():
    import ast

    root = Path(__file__).resolve().parents[1]
    text = (root / "examples" / "egress_agent.py").read_text()
    tree = ast.parse(text)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    wraps = [n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "wrap_egress_client"]
    assert wraps and any(k.arg == "session_span" for k in wraps[0].keywords)
    assert "add_shutdown_callback" in text and "asyncio.wait_for" in text
    assert "EgressClient(" not in text and "api.LiveKitAPI(" in text
    assert "record_egress" in (root / "README.md").read_text()
    assert "credential-free" in (root / "README.md").read_text()
    changelog = (root / "CHANGELOG.md").read_text()
    assert "record_egress" in changelog and "wrap_egress_client" in changelog


def test_recording_docs_check_survives_release_rollover(monkeypatch):
    original_read = Path.read_text

    def released_text(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        return text.replace("Unreleased", "Released") if path.name == "CHANGELOG.md" else text

    monkeypatch.setattr(Path, "read_text", released_text)
    monkeypatch.setattr(traceai_livekit, "__version__", "9.9.9")
    test_egress_example_and_docs_demonstrate_supported_trace_linkage()


@pytest.mark.parametrize("entry", ["manual", "wrapped"])
def test_recording_isolates_ambient_fi_attributes_and_restores_context(harness, entry):
    from fi_instrumentation import get_attributes_from_context, using_attributes, using_session

    h = harness
    _, wrap = _apis()
    existing_policy = TraceConfig()
    existing = FITracer(h.tracer, config=existing_policy)
    saved = trace.NonRecordingSpan(h.root.get_span_context())
    h.root.end()
    service = FakeService(_info())
    proxy = wrap(service, session_span=saved, tracer=existing,
                 trace_config=WithholdConversation())
    with h.tracer.start_as_current_span("unrelated") as other:
        with using_session("ambient-person@example.invalid"), using_attributes(
            user_id="ambient-user@example.invalid", metadata={"source": "ambient-synthetic"},
            tags=["ambient-tag-synthetic"], prompt_template="ambient-template-synthetic",
        ):
            before = context.get_current()
            ambient = dict(get_attributes_from_context())
            sampler_at, starts_at = len(h.sampler.attributes), len(h.starts.attributes)
            if entry == "manual":
                assert _capture(h, session_span=saved, tracer=existing,
                                trace_config=WithholdConversation())
            else:
                assert asyncio.run(proxy.stop_egress(object())) is service.response
            assert context.get_current() is before
            assert dict(get_attributes_from_context()) == ambient
            assert trace.get_current_span() is other
            assert existing._self_config is existing_policy

    raw, = _recordings(h.raw)
    mapped, = _recordings(h.mapped)
    assert raw.parent.span_id == saved.get_span_context().span_id
    assert raw.context.trace_id == saved.get_span_context().trace_id
    assert raw.context.trace_id != other.get_span_context().trace_id
    observed = (h.sampler.attributes[sampler_at:] + h.starts.attributes[starts_at:]
                + [dict(raw.attributes), dict(mapped.attributes)])
    assert len(observed) == 4
    for attrs in observed:
        assert not set(ambient).intersection(attrs)
        assert "lk.pii.recording.conversation_id" not in attrs
        assert "ambient-" not in str(attrs)
    assert mapped.attributes["livekit.recording.room_name"] == "room-synthetic"


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt], ids=["exception", "baseexception"])
def test_recording_creation_failure_restores_ambient_context(harness, monkeypatch, error_type):
    from fi_instrumentation import get_attributes_from_context, using_session

    observed = []
    parents = []

    def fail_start(self, *args, **kwargs):
        observed.append(dict(get_attributes_from_context()))
        parents.append(trace.get_current_span().get_span_context())
        raise error_type("synthetic-start-failure")

    monkeypatch.setattr(FITracer, "start_span", fail_start)
    with using_session("ambient-person@example.invalid"):
        before = context.get_current()
        ambient = dict(get_attributes_from_context())
        if error_type is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt):
                _capture(harness)
        else:
            assert _capture(harness) is False
        assert context.get_current() is before
        assert dict(get_attributes_from_context()) == ambient
    assert observed == [{}]
    assert parents == [harness.root.get_span_context()]
    assert _recordings(harness.raw) == []


ROOM_MASK_CASES = [
    pytest.param("lk.pii.room_name", None, id="source-withheld"),
    pytest.param("lk.pii.room_name", "room-redacted-synthetic", id="source-redacted"),
    pytest.param("livekit.recording.room_name", None, id="alias-withheld"),
    pytest.param("livekit.recording.room_name", "room-redacted-synthetic", id="alias-redacted"),
]


def _key_policy(key, replacement):
    class KeyPolicy(TraceConfig):
        def mask(self, candidate, value):
            if candidate == key:
                return replacement
            return super().mask(candidate, value)

    return KeyPolicy()


def _assert_observer_value(h, source, alias, expected):
    raw, = _recordings(h.raw)
    mapped, = _recordings(h.mapped)
    for attrs in (h.sampler.attributes[-1], h.starts.attributes[-1], raw.attributes, mapped.attributes):
        if expected is None:
            assert source not in attrs
        else:
            assert attrs[source] == expected
    for attrs in (h.sampler.attributes[-1], h.starts.attributes[-1], raw.attributes):
        assert alias not in attrs
    if expected is None:
        assert alias not in mapped.attributes
    else:
        assert mapped.attributes[alias] == expected


@pytest.mark.parametrize("key,replacement", ROOM_MASK_CASES)
def test_implicit_conversation_uses_only_fully_masked_room(harness, key, replacement):
    assert _capture(harness, _info(room_name="person@example.invalid"),
                    trace_config=_key_policy(key, replacement))
    _assert_observer_value(harness, "lk.pii.room_name", "livekit.recording.room_name", replacement)
    _assert_observer_value(harness, "lk.pii.recording.conversation_id",
                           SpanAttributes.GEN_AI_CONVERSATION_ID, replacement)
    for attrs in (harness.sampler.attributes[-1], harness.starts.attributes[-1],
                  _recordings(harness.raw)[0].attributes, _recordings(harness.mapped)[0].attributes):
        assert "person@example.invalid" not in str(dict(attrs))


@pytest.mark.parametrize("key,replacement", ROOM_MASK_CASES)
@pytest.mark.parametrize("withhold_explicit", [False, True], ids=["explicit-kept", "explicit-withheld"])
def test_explicit_conversation_is_independent_of_room_mask(harness, key, replacement, withhold_explicit):
    existing = FITracer(harness.tracer, config=WithholdConversation() if withhold_explicit else TraceConfig())
    assert _capture(harness, _info(room_name="person@example.invalid"), tracer=existing,
                    trace_config=_key_policy(key, replacement), conversation_id="conversation-synthetic")
    _assert_observer_value(harness, "lk.pii.room_name", "livekit.recording.room_name", replacement)
    _assert_observer_value(harness, "lk.pii.recording.conversation_id", SpanAttributes.GEN_AI_CONVERSATION_ID,
                           None if withhold_explicit else "conversation-synthetic")


@pytest.mark.parametrize("replacement", [None, "room-final-synthetic"], ids=["withheld", "redacted"])
def test_implicit_conversation_obeys_both_room_policies(harness, replacement):
    helper_policy = _key_policy("livekit.recording.room_name", "room-first-synthetic")
    existing_policy = _key_policy("lk.pii.room_name", replacement)
    assert _capture(harness, _info(room_name="person@example.invalid"),
                    tracer=FITracer(harness.tracer, config=existing_policy), trace_config=helper_policy)
    _assert_observer_value(harness, "lk.pii.room_name", "livekit.recording.room_name", replacement)
    _assert_observer_value(harness, "lk.pii.recording.conversation_id",
                           SpanAttributes.GEN_AI_CONVERSATION_ID, replacement)


def _capture_with_limits(policy, *, limit=None, sampler=None):
    from opentelemetry.sdk.trace import SpanLimits

    sampler = sampler if sampler is not None else ObserveSampler()
    limits = {} if limit is None else {"span_limits": SpanLimits(max_attributes=limit)}
    provider = TracerProvider(sampler=sampler, **limits)
    raw, mapped, starts = InMemorySpanExporter(), MappedMemory(), OnStart()
    provider.add_span_processor(starts)
    provider.add_span_processor(SimpleSpanProcessor(mapped))
    provider.add_span_processor(SimpleSpanProcessor(raw))
    tracer = provider.get_tracer("caller-supplied-recording-scope")
    root = tracer.start_span("session-envelope")
    try:
        record, _ = _apis()
        assert record(Info(egress_id="egress-synthetic", room_name="person@example.invalid"),
                      session_span=root, tracer=tracer, trace_config=policy)
        return Info(raw=_recordings(raw)[0], mapped=_recordings(mapped)[0],
                    start=starts.attributes[-1], sampled=sampler.attributes[-1])
    finally:
        root.end()
        provider.shutdown()


@pytest.mark.parametrize("limit", [0, 1, 2, 3], ids=["limit-0", "limit-1", "limit-2", "limit-3"])
@pytest.mark.parametrize("replacement", [None, "conversation-redacted-synthetic"], ids=["withheld", "redacted"])
def test_recording_marker_eviction_does_not_resurrect_conversation(limit, replacement):
    h = _capture_with_limits(_key_policy(SpanAttributes.GEN_AI_CONVERSATION_ID, replacement), limit=limit)
    # max_attributes=2 with withholding retains room+kind, not the marker.
    if limit <= 2 or replacement is not None:
        assert "lk.recording.operation" not in h.raw.attributes
        assert "lk.recording.operation" not in h.start
    if limit == 2 and replacement is None:
        assert h.raw.attributes["lk.pii.room_name"] == "person@example.invalid"
    source = "lk.pii.recording.conversation_id"
    for attrs in (h.sampled, h.start, h.raw.attributes):
        assert SpanAttributes.GEN_AI_CONVERSATION_ID not in attrs
        if source in attrs:
            assert replacement is not None and attrs[source] == replacement
    if source in h.raw.attributes:
        assert h.mapped.attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == replacement
    else:
        assert SpanAttributes.GEN_AI_CONVERSATION_ID not in h.mapped.attributes


@pytest.mark.parametrize("replacement", [None, "conversation-redacted-synthetic"], ids=["withheld", "redacted"])
def test_sampler_dropped_recording_marker_does_not_resurrect_conversation(replacement):
    from opentelemetry.sdk.trace.sampling import SamplingResult

    class DropMarkerSampler(ObserveSampler):
        def should_sample(self, parent_context, trace_id, name, kind=None,
                          attributes=None, links=None, trace_state=None):
            result = super().should_sample(parent_context, trace_id, name, kind,
                                           attributes, links, trace_state)
            retained = {key: value for key, value in (result.attributes or {}).items()
                        if key != "lk.recording.operation"}
            return SamplingResult(result.decision, retained, result.trace_state)

    h = _capture_with_limits(_key_policy(SpanAttributes.GEN_AI_CONVERSATION_ID, replacement),
                             sampler=DropMarkerSampler())
    assert "lk.recording.operation" in h.sampled
    for attrs in (h.start, h.raw.attributes, h.mapped.attributes):
        assert "lk.recording.operation" not in attrs
        assert attrs["lk.pii.room_name"] == "person@example.invalid"
    assert "livekit.recording.operation" not in h.mapped.attributes
    assert h.mapped.attributes["livekit.recording.room_name"] == "person@example.invalid"
    assert SpanAttributes.GEN_AI_CONVERSATION_ID not in h.raw.attributes
    if replacement is None:
        assert SpanAttributes.GEN_AI_CONVERSATION_ID not in h.mapped.attributes
    else:
        assert h.mapped.attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == replacement
