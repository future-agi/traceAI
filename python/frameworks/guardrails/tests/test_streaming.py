"""Real streamed validators through LiteLLM/OpenAI and a loopback SSE provider."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from threading import Thread

import pytest
from fi_instrumentation import TraceConfig
from guardrails import AsyncGuard, Guard
from guardrails.validators import PassResult, Validator, register_validator
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_guardrails import GuardrailsInstrumentor


class SpanAccounting(SpanProcessor):
    def __init__(self):
        self.live = set()

    def on_start(self, span, parent_context=None):
        self.live.add(span.context.span_id)

    def on_end(self, span):
        self.live.remove(span.context.span_id)


@pytest.fixture
def streaming(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("GUARDRAILS_NO_METRICS", "true")
    (tmp_path / ".guardrailsrc").write_text("no_metrics=true\n")
    from guardrails.settings import settings

    if hasattr(settings, "rc"):
        monkeypatch.setattr(settings.rc, "enable_metrics", False)
        monkeypatch.setattr(Guard, "_load_rc", lambda self: None)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    accounting = SpanAccounting()
    provider.add_span_processor(accounting)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    requests = []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                ({"role": "assistant", "content": "PRIVATE_STREAM_OUTPUT "}, None),
                ({"content": "complete."}, None),
                ({}, "stop"),
            ]
            try:
                for delta, finish in chunks:
                    chunk = {
                        "id": "chatcmpl-synthetic", "object": "chat.completion.chunk",
                        "created": 1, "model": "gpt-4o-mini",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # A caller may intentionally close after the first chunk.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    instrumentor = GuardrailsInstrumentor()

    def start(**privacy):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**privacy))

    yield start, provider, exporter, accounting, requests, f"http://127.0.0.1:{server.server_port}/v1"
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    server.shutdown()
    server.server_close()
    thread.join()
    provider.shutdown()


def stream_guard(asynchronous=False, failure=None):
    @register_validator(name="stream-privacy", data_type="string")
    class StreamingValidator(Validator):
        def _validate(self, value, metadata):
            if failure:
                raise failure("PRIVATE_STREAM_FAILURE")
            return PassResult()

    validator = StreamingValidator(on_fail="noop")
    # Custom chunking is supported by Guardrails and avoids NLTK downloads.
    validator._chunking_function = lambda text: [text, ""]
    guard = (AsyncGuard if asynchronous else Guard)().use(validator)
    guard.configure(allow_metrics_collection=False)
    return guard


def options(url, asynchronous):
    import litellm

    return {
        "llm_api": litellm.acompletion if asynchronous else litellm.completion,
        "model": "openai/gpt-4o-mini", "api_base": url,
        "api_key": "sk-SYNTHETIC_STREAM_CREDENTIAL",
        "messages": [{"role": "user", "content": "PRIVATE_STREAM_PROMPT"}],
        "stream": True, "num_reasks": 0, "num_retries": 0, "timeout": 5,
    }


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
def test_stream_validator_wire_privacy(streaming, monkeypatch, asynchronous, environment, hide_inputs, hide_outputs):
    start, provider, exporter, accounting, requests, url = streaming
    if environment:
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hide_inputs).lower())
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hide_outputs).lower())
        start()
    else:
        start(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    guard = stream_guard(asynchronous)

    async def run():
        stream = await guard(**options(url, True))
        return [part async for part in stream]

    before = context.get_current()
    with provider.get_tracer("application").start_as_current_span("application") as parent:
        # These deliberately use the SDK's aliases/suffix but belong to the app.
        parent.add_event("application_result", {"input": "app input", "output": "app output"})
        results = asyncio.run(run()) if asynchronous else list(guard(**options(url, False)))
        assert trace.get_current_span() is parent
    assert context.get_current() is before
    assert requests and results and not accounting.live
    assert "PRIVATE_STREAM_OUTPUT " in "".join(part.raw_llm_output or "" for part in results)
    spans = exporter.get_finished_spans()
    application, = [span for span in spans if span.name == "application"]
    assert len(application.events) == 1
    assert dict(application.events[0].attributes) == {"input": "app input", "output": "app output"}
    children = [span for span in spans if span.name != "application"]
    assert children
    assert all(span.context.trace_id == parent.get_span_context().trace_id for span in children)
    assert all(span.parent.span_id in {s.context.span_id for s in spans} for span in children)
    if find_spec("guardrails.telemetry.guard_tracing") is None:
        events = [event for span in children for event in span.events if event.name == "stream-privacy_result"]
        assert events
        for event in events:
            attrs = event.attributes
            assert "input" not in attrs and "output" not in attrs
            assert (attrs["input.value"] == "__REDACTED__") is hide_inputs
            assert (attrs["output.value"] == "__REDACTED__") is hide_outputs
    encoded = encode_spans(spans).SerializeToString()
    assert b"sk-SYNTHETIC_STREAM_CREDENTIAL" not in encoded
    if hide_inputs:
        assert b"PRIVATE_STREAM_PROMPT" not in encoded
    if hide_inputs and hide_outputs:
        assert b"PRIVATE_STREAM_OUTPUT" not in encoded
    elif not hide_inputs or not hide_outputs:
        assert b"PRIVATE_STREAM_OUTPUT" in encoded


@pytest.mark.parametrize("asynchronous", [False, True])
def test_legacy_stream_yield_and_close_restore_application_context(streaming, asynchronous):
    if find_spec("guardrails.telemetry.guard_tracing") is not None:
        pytest.skip("Legacy generator boundaries; native lifecycle belongs to Guardrails")
    start, provider, exporter, accounting, requests, url = streaming
    start(hide_inputs=True, hide_outputs=True)
    guard = stream_guard(asynchronous)

    async def run(parent):
        stream = await guard(**options(url, True))
        first = await anext(stream)
        assert trace.get_current_span() is parent
        assert accounting.live == {parent.get_span_context().span_id}
        await stream.aclose()
        assert trace.get_current_span() is parent
        assert accounting.live == {parent.get_span_context().span_id}
        return first

    before = context.get_current()
    with provider.get_tracer("application").start_as_current_span("application") as parent:
        if asynchronous:
            first = asyncio.run(run(parent))
        else:
            stream = guard(**options(url, False))
            first = next(stream)
            assert trace.get_current_span() is parent
            assert accounting.live == {parent.get_span_context().span_id}
            stream.close()
        assert trace.get_current_span() is parent
        parent.add_event("consumer_result", {"input": "app input", "output": "app output"})
    assert first.raw_llm_output == "PRIVATE_STREAM_OUTPUT "
    assert context.get_current() is before and not accounting.live
    spans = exporter.get_finished_spans()
    assert requests and spans
    application, = [span for span in spans if span.name == "application"]
    assert [event.name for event in application.events] == ["consumer_result"]
    assert b"PRIVATE_" not in encode_spans(spans).SerializeToString()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
def test_legacy_stream_failure_closes_spans(streaming, asynchronous, cancelled):
    if find_spec("guardrails.telemetry.guard_tracing") is not None:
        pytest.skip("Legacy generator boundaries; native lifecycle belongs to Guardrails")
    start, provider, exporter, accounting, requests, url = streaming
    start(hide_inputs=True, hide_outputs=True)
    failure = asyncio.CancelledError if cancelled else ValueError
    guard = stream_guard(asynchronous, failure)

    async def run():
        stream = await guard(**options(url, True))
        return [part async for part in stream]

    before = context.get_current()
    with provider.get_tracer("application").start_as_current_span("application") as parent:
        with pytest.raises(failure, match="PRIVATE_STREAM_FAILURE"):
            if asynchronous:
                asyncio.run(run())
            else:
                list(guard(**options(url, False)))
        assert trace.get_current_span() is parent
        assert accounting.live == {parent.get_span_context().span_id}
    assert context.get_current() is before and not accounting.live
    assert requests and exporter.get_finished_spans()
    assert b"PRIVATE_" not in encode_spans(exporter.get_finished_spans()).SerializeToString()


def test_concurrent_legacy_streams_keep_separate_application_contexts(streaming):
    if find_spec("guardrails.telemetry.guard_tracing") is not None:
        pytest.skip("Legacy generator boundaries")
    start, provider, exporter, accounting, requests, url = streaming
    start(hide_inputs=True, hide_outputs=True)

    async def consume(index):
        guard = stream_guard(True)
        with provider.get_tracer("application").start_as_current_span(f"application-{index}") as parent:
            stream = await guard(**options(url, True))
            results = []
            async for part in stream:
                assert trace.get_current_span() is parent
                results.append(part)
                await asyncio.sleep(0)
                assert trace.get_current_span() is parent
            assert results
            return parent.get_span_context().trace_id

    async def run():
        return await asyncio.gather(consume(0), consume(1))

    before = context.get_current()
    trace_ids = asyncio.run(run())
    assert len(set(trace_ids)) == 2
    assert context.get_current() is before and not accounting.live
    assert len(requests) == 2
    spans = exporter.get_finished_spans()
    assert all(span.context.trace_id in trace_ids for span in spans)
    for trace_id in trace_ids:
        members = [span for span in spans if span.context.trace_id == trace_id]
        assert len(members) > 1
        assert all(span.parent is None or span.parent.span_id in {s.context.span_id for s in members} for span in members)
    assert b"PRIVATE_" not in encode_spans(spans).SerializeToString()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_legacy_stream_suppression(streaming, asynchronous):
    if find_spec("guardrails.telemetry.guard_tracing") is not None:
        pytest.skip("Legacy generator boundaries")
    start, _, exporter, accounting, requests, url = streaming
    start(hide_inputs=True, hide_outputs=True)
    guard = stream_guard(asynchronous)

    async def run():
        stream = await guard(**options(url, True))
        return [part async for part in stream]

    token = context.attach(context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        results = asyncio.run(run()) if asynchronous else list(guard(**options(url, False)))
    finally:
        context.detach(token)
    assert results and requests and not accounting.live
    assert exporter.get_finished_spans() == ()
