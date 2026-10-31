"""Deterministic regressions for all 13 final round-two findings and the safety contract."""

import asyncio
import json
import os
import select
import threading
import time
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import _manager, _registry, wire
from fi_instrumentation.auto._config import parse_config_text
from fi_instrumentation.auto.wire import _transports
from fi_instrumentation.auto.wire._capture import Recorder
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_safety import recording, only  # noqa: F401

URL = "https://api.openai.com/v1/chat/completions"
CHAT = {"model": "gpt", "messages": [{"role": "user", "content": "hello"}]}
PREFIX = "gen_ai.output.messages.0.message.tool_calls.0.tool_call"
PARTIAL = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'


def body_capture(tracer, url=URL, request=CHAT):
    capture = Recorder(tracer).begin("POST", url, {}, json.dumps(request).encode())
    assert capture is not None
    return capture


def complete(capture, response, streamed=False):
    capture.on_headers(200, {"content-type": "text/event-stream" if streamed else "application/json"})
    if streamed:
        for event in response:
            capture.on_chunk(b"data: " + json.dumps(event).encode() + b"\n\n")
    else:
        capture.on_chunk(json.dumps(response).encode())
    capture.finish()


@pytest.fixture
def integration_double(fresh_state, monkeypatch):
    import sys

    class Instrumentor:
        active = False
        provider = None
        entered = None
        release = None
        uninstalled = threading.Event()

        @property
        def is_instrumented_by_opentelemetry(self):
            return self.active

        def instrument(self, **kwargs):
            self.provider = kwargs["tracer_provider"]
            if self.entered is not None:
                self.entered.set()
                assert self.release.wait(3)
            self.active = True

        def uninstrument(self):
            self.active = False
            self.uninstalled.set()

        def call(self):
            if self.active:
                with self.provider.get_tracer("library").start_as_current_span("library-call"):
                    pass

    instrumentor = Instrumentor()
    item = _registry.Integration("owned", ("fakeailib",), "fake", "traceai_owned_double", "Instrumentor")
    monkeypatch.setitem(sys.modules, item.module, SimpleNamespace(Instrumentor=lambda: instrumentor))
    monkeypatch.setattr(_registry, "ALL", (item,))
    yield item, instrumentor
    _manager.auto_instrument(instrumentations=[], wire=False)


def test_explicit_base_url_never_constructs_environment_exporter(fresh_state, monkeypatch):
    from fi_instrumentation import otel

    monkeypatch.setenv("FI_BASE_URL", "http://[")
    constructed = []

    class Exporter(InMemorySpanExporter):
        def __init__(self, endpoint=None, **kwargs):
            super().__init__()
            constructed.append(endpoint)

    monkeypatch.setattr(otel, "HTTPSpanExporter", Exporter)
    _manager.init(base_url="https://collector.example", api_key="explicit-key", secret_key="explicit-secret",
                  instrumentations=[], batch=False)
    assert constructed == ["https://collector.example/tracer/v1/traces"]
    _manager.shutdown()


def test_shutdown_deadline_includes_state_lock_contention(fresh_state, monkeypatch):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(_manager, "_shutdown_lock", threading.Lock())
    monkeypatch.setattr(_manager, "_lifecycle_lock", threading.Lock())
    _manager._state.provider = SimpleNamespace(shutdown=stopped.set)
    _manager._state.owned_provider = True

    def hold():
        with _manager._state.lock:
            entered.set()
            assert release.wait(3)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1)
    try:
        start = time.monotonic()
        _manager.shutdown(10)
        assert time.monotonic() - start < 0.5
        assert not stopped.is_set()
    finally:
        release.set()
        holder.join(1)
    assert stopped.wait(1)


def test_additive_call_cannot_cancel_install_in_progress(integration_double):
    item, instrumentor = integration_double
    instrumentor.entered, instrumentor.release = threading.Event(), threading.Event()
    provider = TracerProvider(shutdown_on_exit=False)
    installer = threading.Thread(target=lambda: _manager.auto_instrument(
        tracer_provider=provider, instrumentations=[item.name], wire=False, lazy=False))
    installer.start()
    assert instrumentor.entered.wait(1)
    try:
        assert _manager.auto_instrument(instrumentations=[], wire=False)[item.name] == _manager.PENDING
    finally:
        instrumentor.release.set()
        installer.join(2)
    assert not installer.is_alive()
    assert instrumentor.active
    assert {s.name: s.status for s in _manager.report()}[item.name] == _manager.INSTRUMENTED
    _manager.shutdown()
    provider.shutdown()


def test_shutdown_removes_owned_integration_without_closing_app_provider(integration_double):
    item, instrumentor = integration_double
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _manager.init(tracer_provider=provider, instrumentations=[item.name], wire=False, lazy=False)
    instrumentor.call()
    _manager.shutdown(1000)
    assert instrumentor.uninstalled.is_set()
    instrumentor.call()
    with provider.get_tracer("app").start_as_current_span("app-still-running"):
        pass
    assert [s.name for s in exporter.get_finished_spans()] == ["library-call", "app-still-running"]
    provider.shutdown()


def test_shutdown_invalidates_pending_import_callback(integration_double):
    item, instrumentor = integration_double
    provider = TracerProvider(shutdown_on_exit=False)
    assert _manager.auto_instrument(tracer_provider=provider, wire=False)[item.name] == _manager.PENDING
    _manager.shutdown(1000)
    __import__("fakeailib")
    assert not instrumentor.active
    assert {s.name: s.status for s in _manager.report()}[item.name] == _manager.DISABLED
    provider.shutdown()


def test_adopted_application_instrumentation_is_never_uninstalled(integration_double):
    item, instrumentor = integration_double
    instrumentor.active = True
    provider = TracerProvider(shutdown_on_exit=False)
    _manager.auto_instrument(tracer_provider=provider, wire=False, lazy=False)
    assert {s.name: s.detail for s in _manager.report()}[item.name] == "already instrumented"
    _manager.auto_instrument(instrumentations=[], wire=False)
    assert instrumentor.active
    _manager.shutdown(1000)
    assert instrumentor.active
    assert not instrumentor.uninstalled.is_set()
    provider.shutdown()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is Unix-only")
def test_fork_resets_inflight_lifecycle_and_overlay_mutexes(fresh_state, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_pid = os.getpid()

    class Provider:
        def force_flush(self, timeout_millis):
            if os.getpid() == original_pid:
                entered.set()
                assert release.wait(3)
            return True

    monkeypatch.setattr(_manager, "_lifecycle_lock", threading.Lock())
    _manager._state.provider = Provider()
    assert not _manager.flush(10)
    assert entered.is_set()
    from opentelemetry.sdk.resources import Resource

    overlay = _manager._ResourceOverlayProcessor(SimpleNamespace(force_flush=lambda timeout: True), Resource({}))
    _manager._state.overlays[object()] = overlay
    inherited = [_manager._shutdown_lock, overlay._operation, overlay._shutdown_operation, wire._lock]
    for lock in inherited:
        assert lock.acquire(blocking=False)
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(reader)
            reset = _manager._shutdown_lock.acquire(blocking=False) and wire._lock.acquire(blocking=False)
            ok = reset and _manager.flush(1000) and overlay.force_flush(1000)
            os.write(writer, b"yes" if ok else b"no")
        finally:
            os._exit(0)
    os.close(writer)
    try:
        assert select.select([reader], [], [], 2)[0]
        assert os.read(reader, 3) == b"yes"
    finally:
        release.set()
        for lock in inherited:
            lock.release()
        _manager._state.overlays.clear()
        os.close(reader)
        os.waitpid(child, 0)
    assert _manager._lifecycle_lock.acquire(timeout=1)
    _manager._lifecycle_lock.release()


@pytest.mark.parametrize("streamed", [False, True])
def test_ollama_tool_only_output(recording, streamed):
    exporter, tracer = recording
    capture = body_capture(tracer, "http://localhost:11434/api/chat")
    item = {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "weather", "arguments": {"city": "Paris"}}}]}, "done": True}
    if streamed:
        capture.on_headers(200, {"content-type": "application/x-ndjson"})
        capture.on_chunk(json.dumps(item).encode() + b"\n")
        capture.finish()
    else:
        complete(capture, item)
    attrs = only(exporter).attributes
    assert attrs[PREFIX + ".function.name"] == "weather"
    assert json.loads(attrs[PREFIX + ".function.arguments"]) == {"city": "Paris"}
    assert "weather" in attrs["output.value"]


@pytest.mark.parametrize("streamed", [False, True])
def test_cohere_v1_tool_only_output(recording, streamed):
    exporter, tracer = recording
    capture = body_capture(tracer, "https://api.cohere.com/v1/chat", {"message": "hello"})
    calls = [{"name": "weather", "parameters": {"city": "Paris"}}]
    if streamed:
        complete(capture, [{"event_type": "tool-calls-generation", "tool_calls": calls},
                           {"event_type": "stream-end", "finish_reason": "COMPLETE", "response": {}}], True)
    else:
        complete(capture, {"text": "", "tool_calls": calls})
    attrs = only(exporter).attributes
    assert attrs[PREFIX + ".function.name"] == "weather"
    assert json.loads(attrs[PREFIX + ".function.arguments"]) == {"city": "Paris"}
    assert "weather" in attrs["output.value"]
    if streamed:
        assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("COMPLETE",)


@pytest.mark.parametrize("streamed", [False, True])
def test_anthropic_blocks_join_newline_but_deltas_do_not(recording, streamed):
    exporter, tracer = recording
    capture = body_capture(tracer, "https://api.anthropic.com/v1/messages")
    blocks = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
    if streamed:
        events = []
        for i, block in enumerate(blocks):
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            for delta in [block["text"][:2], block["text"][2:]]:
                events.append({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": delta}})
        complete(capture, events, True)
    else:
        complete(capture, {"role": "assistant", "content": blocks})
    assert only(exporter).attributes["output.value"] == "first\nsecond"


def test_bedrock_invoke_json_null_is_not_decode_failure(recording):
    exporter, tracer = recording
    capture = body_capture(tracer, "https://bedrock-runtime.us-east-1.amazonaws.com/model/model-id/invoke")
    complete(capture, None)
    assert only(exporter).attributes["output.value"] == "null"
    assert only(exporter).attributes["output.mime_type"] == "application/json"


def test_repeated_init_keeps_explicit_semantic_convention(fresh_state):
    from fi_instrumentation.otel import SemanticConvention

    exporter = InMemorySpanExporter()
    options = dict(span_exporter=exporter, batch=False, instrumentations=[],
                   semantic_convention=SemanticConvention.OPENINFERENCE)
    provider = _manager.init(**options)
    for name in ("first", "second"):
        if name == "second":
            assert _manager.init(**options) is provider
        with provider.get_tracer("app").start_as_current_span(name):
            pass
    assert [s.resource.attributes["semantic_convention"] for s in exporter.get_finished_spans()] == ["openinference"] * 2
    _manager.shutdown()


def test_config_closing_quote_after_escaped_backslash():
    assert parse_config_text(r'project_name = "C:\\" # trailing comment')["project_name"] == "C:\\"
    assert parse_config_text(r'project_name = "escaped\"#still string" # comment')["project_name"] == 'escaped"#still string'


@pytest.mark.parametrize("transport", ["httpx", "httpx2", "requests", "aiohttp"])
@pytest.mark.parametrize("complete_length", [False, True])
def test_consumer_exception_is_abort_not_transport_error(recording, transport, complete_length):
    exporter, tracer = recording
    capture = body_capture(tracer)
    headers = {"content-type": "text/event-stream"}
    if complete_length:
        headers["content-length"] = str(len(PARTIAL))
    capture.on_headers(200, headers)
    abort = ValueError("consumer's own exception")
    if transport in ("httpx", "httpx2"):
        module = pytest.importorskip(transport)
        tee = _transports._sync_tee_class(module)(iter([PARTIAL, PARTIAL]), capture)
        chunks = iter(tee)
        assert next(chunks) == PARTIAL
        with pytest.raises(ValueError) as caught:
            chunks.throw(abort)
        assert caught.value is abort
    elif transport == "requests":
        raw = SimpleNamespace(stream=lambda *args, **kwargs: iter([PARTIAL, PARTIAL]))
        chunks = _transports._RawTee(raw, capture).stream()
        assert next(chunks) == PARTIAL
        with pytest.raises(ValueError) as caught:
            chunks.throw(abort)
        assert caught.value is abort
    else:
        pytest.importorskip("aiohttp")

        async def run():
            async def inner():
                yield PARTIAL
                yield PARTIAL
            chunks = _transports._tee_async_iterator(inner(), capture)
            assert await chunks.__anext__() == PARTIAL
            with pytest.raises(ValueError) as caught:
                await chunks.athrow(abort)
            assert caught.value is abort
        asyncio.run(run())
    span = only(exporter)
    assert span.status.status_code == trace.StatusCode.OK
    assert "error.type" not in span.attributes
    assert bool(span.attributes.get("traceai.wire.incomplete")) is not complete_length
    assert span.attributes["output.value"] == "partial"


@pytest.mark.parametrize("client", ["httpx", "httpx2"])
def test_async_httpx_consumer_abort(recording, client):
    module = pytest.importorskip(client)
    exporter, tracer = recording
    capture = body_capture(tracer)
    capture.on_headers(200, {"content-type": "text/event-stream"})

    async def run():
        async def inner():
            yield PARTIAL
            yield PARTIAL
        chunks = _transports._async_tee_class(module)(inner(), capture).__aiter__()
        assert await chunks.__anext__() == PARTIAL
        error = RuntimeError("consumer")
        with pytest.raises(RuntimeError) as caught:
            await chunks.athrow(error)
        assert caught.value is error
    asyncio.run(run())
    assert only(exporter).status.status_code == trace.StatusCode.OK
    assert only(exporter).attributes["traceai.wire.incomplete"]


@pytest.mark.parametrize("ended", ["eof", "length"])
def test_fully_consumed_body_close_is_complete(recording, ended):
    exporter, tracer = recording
    capture = body_capture(tracer)
    capture.on_headers(200, {"content-type": "text/event-stream", "content-length": str(len(PARTIAL))})
    iterator = _transports._tee_iterator(iter([PARTIAL]), capture)
    assert next(iterator) == PARTIAL
    if ended == "eof":
        assert list(iterator) == []
    iterator.close()
    capture.finish(incomplete=True)
    assert "traceai.wire.incomplete" not in only(exporter).attributes


@pytest.mark.parametrize("use_fi_tracer", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_export_boundary_privacy_covers_library_attributes_status_and_events(fresh_state, use_fi_tracer, hidden):
    exporter, app_exporter = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    trace.set_tracer_provider(provider)
    config = TraceConfig(hide_inputs=hidden, hide_outputs=hidden)
    _manager.init(span_exporter=exporter, batch=False, instrumentations=["wire"], config=config,
                  api_key="CONFIGURED_SECRET_123", secret_key="SECOND_SECRET_123")
    tracer = provider.get_tracer("library")
    if use_fi_tracer:
        tracer = FITracer(tracer, config=config)
    media = "AbCd0123" * 80
    secret = "REQUEST_CREDENTIAL_123"
    payload = json.dumps({"authorization_token": "BODY_SECRET", "image": "data:image/png;base64," + media,
                          "url": "https://example.com/?sig=SIGNED_SECRET", "echo": secret})
    with tracer.start_as_current_span("library-call", attributes={"gen_ai.span.kind": "LLM"}) as span:
        # The library span suppresses wire capture, but request header credentials
        # still need to be removed from its own exported errors/content.
        assert wire._recorder.begin("POST", URL, {"Authorization": "Bearer " + secret}, json.dumps(CHAT).encode()) is None
        raw = getattr(span, "__wrapped__", span)
        raw.set_attributes({"input.value": "PRIVATE_INPUT", "output.value": "PRIVATE_OUTPUT",
                            "custom.payload": payload, "echo": "CONFIGURED_SECRET_123",
                            "gen_ai.request.parameters": payload})
        raw.add_event("exception", {"exception.type": "ValueError", "exception.message": payload,
                                    "input.value": "PRIVATE_INPUT", "output.value": "PRIVATE_OUTPUT"})
        raw.set_status(trace.Status(trace.StatusCode.ERROR, payload))
    exported = only(exporter)
    text = str(dict(exported.attributes)) + str(exported.status.description) + str([dict(e.attributes) for e in exported.events])
    for forbidden in [media, secret, "BODY_SECRET", "SIGNED_SECRET", "CONFIGURED_SECRET_123"]:
        assert forbidden not in text
    if hidden:
        assert "PRIVATE_INPUT" not in text and "PRIVATE_OUTPUT" not in text
        assert exported.status.description is None
        assert "exception.message" not in exported.events[0].attributes
        assert exported.events[0].attributes["input.value"] == "__REDACTED__"
        assert exported.events[0].attributes["output.value"] == "__REDACTED__"
    else:
        assert exported.attributes["input.value"] == "PRIVATE_INPUT"
        assert "<redacted>" in text and "<omitted:" in text
    if not use_fi_tracer:
        # Global/native tracers receive the same privacy snapshot as supplied tracers.
        assert secret not in only(app_exporter).attributes["custom.payload"]
    _manager.shutdown()
    provider.shutdown()


def test_fi_tracer_privacy_even_without_agent_export_route(recording):
    exporter, tracer = recording
    with tracer.start_as_current_span("library") as span:
        span.set_attribute("payload", '{"password":"PRIVATE_PASSWORD"}')
        span.record_exception(ValueError("data:image/png;base64," + "A" * 400))
        span.set_status(trace.Status(trace.StatusCode.ERROR, "https://example.com/?api_key=PRIVATE_KEY"))
    span = only(exporter)
    text = str(dict(span.attributes)) + str(span.status.description) + str([dict(e.attributes) for e in span.events])
    assert "PRIVATE_PASSWORD" not in text and "PRIVATE_KEY" not in text and "A" * 400 not in text


def test_shutdown_deadline_during_integration_teardown(integration_double, monkeypatch):
    item, instrumentor = integration_double
    entered, release = threading.Event(), threading.Event()
    original = instrumentor.uninstrument

    def blocked_teardown():
        entered.set()
        assert release.wait(3)
        original()

    monkeypatch.setattr(instrumentor, "uninstrument", blocked_teardown)
    provider = TracerProvider(shutdown_on_exit=False)
    _manager.auto_instrument(tracer_provider=provider, wire=False, lazy=False)
    disabling = threading.Thread(target=lambda: _manager.shutdown(10))
    disabling.start()
    assert entered.wait(1)
    try:
        start = time.monotonic()
        _manager.shutdown(10)
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
        disabling.join(2)
    assert not disabling.is_alive()
    assert instrumentor.uninstalled.wait(2)
    assert not instrumentor.active
    provider.shutdown()


@pytest.mark.parametrize("url", [
    "https://generativelanguage.googleapis.com/v1/models/gemini:streamGenerateContent",
    "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/l/publishers/anthropic/models/model:streamRawPredict",
    "https://bedrock-runtime.us-east-1.amazonaws.com/model/model-id/converse-stream",
    "https://bedrock-runtime.us-east-1.amazonaws.com/model/model-id/invoke-with-response-stream",
])
def test_streaming_endpoint_sets_stream_without_event_framing(recording, url):
    exporter, tracer = recording
    request = {"contents": [{"parts": [{"text": "hello"}]}]} if "gemini" in url else CHAT
    complete(body_capture(tracer, url, request), [])
    assert only(exporter).attributes["gen_ai.request.stream"] is True


@pytest.mark.parametrize("flag,side", [
    ("hide_inputs", "input"), ("hide_input_text", "input"), ("hide_input_messages", "input"),
    ("hide_outputs", "output"), ("hide_output_text", "output"), ("hide_output_messages", "output"),
])
def test_library_event_aliases_obey_each_side_hide_flag(recording, flag, side):
    exporter, base = recording
    tracer = FITracer(base.__wrapped__, config=TraceConfig(**{flag: True}))
    with tracer.start_as_current_span("library") as span:
        raw = span.__wrapped__
        raw.add_event("retry", {"input.value": "PRIVATE_INPUT", "output.value": "PRIVATE_OUTPUT",
                                "gen_ai.input.messages.0.message.tool_calls.0.tool_call.function.arguments": "PRIVATE_INPUT"})
    attrs = only(exporter).events[0].attributes
    assert attrs[side + ".value"] == "__REDACTED__"
    assert ("PRIVATE_INPUT" if side == "input" else "PRIVATE_OUTPUT") not in str(dict(attrs))


def test_resource_overlay_metadata_is_scrubbed_before_export(fresh_state):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=[],
                             metadata={"password": "PRIVATE_PASSWORD", "url": "https://host/?sig=PRIVATE_SIGNATURE"})
    with provider.get_tracer("library").start_as_current_span("call"):
        pass
    text = str(dict(only(exporter).resource.attributes))
    assert "PRIVATE_PASSWORD" not in text and "PRIVATE_SIGNATURE" not in text
    _manager.shutdown()


def test_buffered_library_spans_are_remasked_at_actual_export(fresh_state):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=[],
                             config=TraceConfig(hide_inputs=True, hide_outputs=True))
    overlay = _manager._state.overlays[provider]
    original = overlay._delegate
    queued = []

    class Buffered:
        def on_end(self, span):
            queued.append(span)

        def force_flush(self, timeout):
            original.span_exporter.export(queued)
            queued.clear()
            return True

        def shutdown(self):
            original.shutdown()

    overlay._delegate = Buffered()
    with provider.get_tracer("library").start_as_current_span("queued") as span:
        span.set_attribute("input.value", "PRIVATE_INPUT")
        span.add_event("retry", {"output.value": "PRIVATE_OUTPUT"})
        span.set_status(trace.Status(trace.StatusCode.ERROR, "PRIVATE_INPUT"))
    assert len(queued) == 1 and not exporter.get_finished_spans()
    # Simulate a mapper/custom processor writing after enqueue: the final boundary
    # must still scrub this snapshot immediately before serialization.
    queued[0]._attributes = {"input.value": "PRIVATE_INPUT"}
    assert _manager.flush(1000)
    span = only(exporter)
    assert span.attributes["input.value"] == "__REDACTED__"
    assert span.events[0].attributes["output.value"] == "__REDACTED__"
    assert span.status.description is None
    _manager.shutdown()


def test_failed_privacy_cannot_reexport_raw_live_span_at_shutdown(monkeypatch):
    from fi_instrumentation import otel
    from fi_instrumentation.auto.wire import _privacy

    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(otel.SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("library"), config=TraceConfig())

    def fail(*args):
        raise RuntimeError("privacy failure")

    monkeypatch.setattr(_privacy, "protect_live_span", fail)
    span = tracer.start_span("do-not-export")
    span.set_attribute("input.value", "PRIVATE_INPUT")
    span.end()
    assert not span.is_recording()
    provider.shutdown()
    assert not exporter.get_finished_spans()
