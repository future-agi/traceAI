"""Offline regressions for the SDK's streaming model entry point."""

import json
from functools import wraps
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace

import smolagents
from fi_instrumentation import TraceConfig
from opentelemetry import context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from smolagents import OpenAIServerModel
from traceai_smolagents import SmolagentsInstrumentor
from traceai_smolagents._wrappers import (
    GEN_AI_OUTPUT_MESSAGES,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SPAN_KIND,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GEN_AI_USAGE_TOTAL_TOKENS,
    MESSAGE_CONTENT,
    OUTPUT_VALUE,
)


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield exporter, provider.get_tracer(__name__), provider
    finally:
        if SmolagentsInstrumentor().is_instrumented_by_opentelemetry:
            SmolagentsInstrumentor().uninstrument()
        provider.shutdown()


def current_id():
    return trace.get_current_span().get_span_context().span_id


def sse_response(request):
    assert json.loads(request.content)["stream"] is True
    events = [
        {"choices": [{"index": 0, "delta": {"content": "streamed "}}]},
        {"choices": [{"index": 0, "delta": {"content": "answer"}}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}},
    ]
    data = "".join(
        "data: " + json.dumps({"id": "offline", "created": 0, "model": "offline-model", **event}) + "\n\n"
        for event in events
    ) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)


@pytest.mark.skipif(not hasattr(OpenAIServerModel, "generate_stream"), reason="SDK predates model streaming")
def test_real_sdk_stream_has_model_output_and_usage(tracing):
    exporter, tracer, provider = tracing
    SmolagentsInstrumentor().instrument(tracer_provider=provider)
    with httpx.Client(transport=httpx.MockTransport(sse_response)) as client:
        model = OpenAIServerModel(
            model_id="offline-model", api_key="test-not-a-secret",
            api_base="http://127.0.0.1:9/v1", client_kwargs={"http_client": client},
        )
        with tracer.start_as_current_span("caller") as caller:
            chunks = list(model.generate_stream(messages=[smolagents.models.ChatMessage(role="user", content="prompt")]))
            assert current_id() == caller.get_span_context().span_id
    assert "".join(chunk.content or "" for chunk in chunks) == "streamed answer"
    spans = exporter.get_finished_spans()
    models = [span for span in spans if span.name.endswith(".generate_stream")]
    assert len(models) == 1
    (span,) = models
    assert span.parent.span_id == caller.get_span_context().span_id
    assert span.attributes[GEN_AI_SPAN_KIND] == "LLM"
    assert span.attributes[GEN_AI_REQUEST_MODEL] == "offline-model"
    assert span.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 11
    assert span.attributes[GEN_AI_USAGE_OUTPUT_TOKENS] == 3
    assert span.attributes[GEN_AI_USAGE_TOTAL_TOKENS] == 14
    assert span.attributes[f"{GEN_AI_OUTPUT_MESSAGES}.0.{MESSAGE_CONTENT}"] == "streamed answer"
    assert json.loads(span.attributes[OUTPUT_VALUE])["content"] == "streamed answer"
    assert span.status.status_code.name == "OK"


@pytest.mark.skipif(not hasattr(OpenAIServerModel, "generate_stream"), reason="SDK predates model streaming")
@pytest.mark.parametrize("agent_class", [smolagents.ToolCallingAgent, smolagents.CodeAgent])
@pytest.mark.parametrize("stream_run", [False, True])
def test_real_streaming_agents_parent_model_to_step(tracing, agent_class, stream_run):
    exporter, tracer, provider = tracing
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        assert requests[-1]["stream"] is True
        if agent_class is smolagents.CodeAgent:
            fragments = [{"content": "<code>\nfinal_answer(42)\n</code>"}]
        else:
            fragments = [
                {"tool_calls": [{"index": 0, "id": "final", "type": "function", "function": {"name": "final_answer", "arguments": '{"answer":'}}]},
                {"tool_calls": [{"index": 0, "function": {"arguments": "42}"}}]},
            ]
        events = [{"choices": [{"index": 0, "delta": fragment}]} for fragment in fragments]
        events.append({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}})
        data = "".join("data: " + json.dumps({"id": "offline", "created": 0, "model": "offline-model", **event}) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    SmolagentsInstrumentor().instrument(tracer_provider=provider)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        agent = agent_class(
            tools=[], max_steps=1, verbosity_level=0, stream_outputs=True,
            model=OpenAIServerModel(model_id="offline-model", api_key="test-not-a-secret", api_base="http://127.0.0.1:9/v1", client_kwargs={"http_client": client}),
        )
        with tracer.start_as_current_span("caller") as caller:
            result = agent.run("Return 42", stream=stream_run)
            if stream_run:
                events = []
                for event in result:
                    events.append(event)
                    assert current_id() == caller.get_span_context().span_id
                assert events[-1].output == 42
            else:
                assert result == 42
    assert len(requests) == 1
    spans = exporter.get_finished_spans()
    (step,) = [s for s in spans if s.name == "Step 1"]
    (model,) = [s for s in spans if s.name.endswith(".generate_stream")]
    assert model.parent.span_id == step.context.span_id
    assert model.attributes[GEN_AI_USAGE_TOTAL_TOKENS] == 14
    assert model.status.status_code.name == "OK"
    assert current_id() == 0


def export_model(monkeypatch, operation):
    class OfflineModel(smolagents.models.Model):
        def __init__(self):
            self.model_id = "offline-model"
            self.kwargs = {}
        # A previous call's legacy usage must not leak into a new stream.
        @property
        def last_input_token_count(self):
            return 999

        @property
        def last_output_token_count(self):
            return 888

        generate_stream = operation

    monkeypatch.setattr(smolagents, "OfflineModel", OfflineModel, raising=False)
    return OfflineModel


def delta(text=None, usage=None, tools=None):
    return SimpleNamespace(content=text, token_usage=usage, tool_calls=tools)


@pytest.mark.parametrize("termination", ["complete", "close", "error", "close_error", "throw"])
def test_model_stream_context_protocol_and_partial_output(tracing, monkeypatch, termination):
    exporter, tracer, provider = tracing
    marker = context.create_key("model-producer")
    seen = []
    failure = ValueError("synthetic stream failure")
    chunks = [delta("first"), delta("second")]

    def operation(self, messages):
        with tracer.start_as_current_span("provider-operation") as child:
            token = context.attach(context.set_value(marker, "producer"))
            try:
                assert (yield chunks[0]) == "sent"
                if termination == "error":
                    raise failure
                yield chunks[1]
                return "finished"
            finally:
                seen.append(current_id())
                assert current_id() == child.get_span_context().span_id
                assert context.get_value(marker) == "producer"
                context.detach(token)
                if termination == "close_error":
                    raise failure

    model_cls = export_model(monkeypatch, operation)
    SmolagentsInstrumentor().instrument(tracer_provider=provider)
    with tracer.start_as_current_span("caller") as caller:
        stream = model_cls().generate_stream([])
        assert next(stream) is chunks[0]
        assert current_id() == caller.get_span_context().span_id
        assert context.get_value(marker) is None
        with tracer.start_as_current_span("consumer") as consumer:
            if termination == "error":
                with pytest.raises(ValueError) as caught:
                    stream.send("sent")
                assert caught.value is failure
            else:
                assert stream.send("sent") is chunks[1]
                if termination == "complete":
                    with pytest.raises(StopIteration) as stopped:
                        next(stream)
                    assert stopped.value.value == "finished"
                elif termination in {"throw", "close_error"}:
                    with pytest.raises(ValueError) as caught:
                        stream.throw(failure) if termination == "throw" else stream.close()
                    assert caught.value is failure
                else:
                    stream.close()
            assert current_id() == consumer.get_span_context().span_id
            assert context.get_value(marker) is None
            stream.close()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert len(exporter.get_finished_spans()) == 4
    span = spans["OfflineModel.generate_stream"]
    assert span.parent.span_id == caller.get_span_context().span_id
    assert spans["provider-operation"].parent.span_id == span.context.span_id
    assert seen == [spans["provider-operation"].context.span_id]
    assert span.status.status_code.name == ("ERROR" if termination in {"error", "throw", "close_error"} else "OK")
    assert json.loads(span.attributes[OUTPUT_VALUE])["content"] == ("first" if termination == "error" else "firstsecond")
    assert GEN_AI_USAGE_INPUT_TOKENS not in span.attributes
    assert GEN_AI_USAGE_OUTPUT_TOKENS not in span.attributes
    assert current_id() == 0


@pytest.mark.parametrize("decorated", [False, True])
def test_interleaved_streams_accumulate_independently_and_restore_methods(tracing, monkeypatch, decorated):
    exporter, tracer, provider = tracing
    calls = []

    def operation(self, messages):
        yield delta(messages[0]["content"], SimpleNamespace(input_tokens=4, output_tokens=1))
        yield delta("!", SimpleNamespace(input_tokens=0, output_tokens=1))

    if decorated:
        original = operation

        @wraps(original)
        def operation(*args, **kwargs):
            calls.append(current_id())
            return map(lambda item: item, original(*args, **kwargs))

    model_cls = export_model(monkeypatch, operation)

    class DerivedModel(model_cls):
        pass

    monkeypatch.setattr(smolagents, "DerivedModel", DerivedModel, raising=False)
    monkeypatch.setattr(smolagents, "AliasModel", model_cls, raising=False)
    instrumentor = SmolagentsInstrumentor()
    for _ in range(2):
        instrumentor.instrument(tracer_provider=provider)
        model = DerivedModel()
        with tracer.start_as_current_span("caller") as caller:
            streams = [model.generate_stream(messages=[]) for _ in range(2)]
            for stream in streams:
                stream.close()  # no model work for an unstarted stream
            streams = [model.generate_stream([{"role": "user", "content": value}]) for value in ("one", "two")]
            for stream in streams:
                next(stream)
                assert current_id() == caller.get_span_context().span_id
            for stream in streams:
                list(stream)
                assert current_id() == caller.get_span_context().span_id
        instrumentor.uninstrument()
        assert model_cls.generate_stream is operation
        assert "generate_stream" not in vars(DerivedModel)
    spans = [s for s in exporter.get_finished_spans() if s.name.endswith(".generate_stream")]
    assert len(spans) == 4  # no duplicates from aliases, inheritance or reinstrumenting
    assert all(s.attributes[GEN_AI_USAGE_TOTAL_TOKENS] == 6 for s in spans)
    assert [json.loads(s.attributes[OUTPUT_VALUE])["content"] for s in spans] == ["one!", "two!", "one!", "two!"]
    if decorated:
        assert set(calls) == {s.context.span_id for s in spans}
    assert current_id() == 0


def test_tools_and_usage_are_accumulated_without_mutating_deltas(tracing, monkeypatch):
    exporter, _, provider = tracing
    def tool(index, name=None, arguments=None, id=None):
        return SimpleNamespace(index=index, id=id, type="function", function=SimpleNamespace(name=name, arguments=arguments))
    chunks = [
        delta(tools=[tool(2, "first", '{"a":', "call-a"), tool(0, "second", '{"b":', "call-b")]),
        delta(tools=[tool(0, arguments="2}"), tool(2, arguments="1}")]),
        delta(usage=SimpleNamespace(input_tokens=7, output_tokens=0)),
        delta(usage=SimpleNamespace(input_tokens=0, output_tokens=3)),
    ]
    def operation(self, messages):
        yield from chunks
    model_cls = export_model(monkeypatch, operation)
    SmolagentsInstrumentor().instrument(tracer_provider=provider)
    received = list(model_cls().generate_stream([]))
    assert all(a is b for a, b in zip(received, chunks))
    assert chunks[0].tool_calls[0].function.arguments == '{"a":'
    (span,) = exporter.get_finished_spans()
    output = json.loads(span.attributes[OUTPUT_VALUE])
    assert output["tool_calls"] == [
        {"id": "call-a", "type": "function", "function": {"name": "first", "arguments": '{"a":1}'}},
        {"id": "call-b", "type": "function", "function": {"name": "second", "arguments": '{"b":2}'}},
    ]
    assert span.attributes[GEN_AI_USAGE_TOTAL_TOKENS] == 10
    assert span.attributes[f"{GEN_AI_OUTPUT_MESSAGES}.0.message.tool_calls.0.tool_call.function.arguments"] == '{"a":1}'


def test_suppression_empty_stream_and_factory_error(tracing, monkeypatch):
    exporter, _, provider = tracing
    calls = []
    def operation(self, messages):
        calls.append(messages)
        return iter(())
    model_cls = export_model(monkeypatch, operation)
    SmolagentsInstrumentor().instrument(tracer_provider=provider)
    token = context.attach(context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert list(model_cls().generate_stream([])) == []
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()
    stream = model_cls().generate_stream([])
    stream.close()
    assert len(calls) == 1
    assert list(model_cls().generate_stream([])) == []
    (span,) = exporter.get_finished_spans()
    assert GEN_AI_USAGE_TOTAL_TOKENS not in span.attributes
    assert span.status.status_code.name == "OK"
    assert json.loads(span.attributes[OUTPUT_VALUE])["content"] == ""
    SmolagentsInstrumentor().uninstrument()

    failure = ValueError("factory failed")
    def fail(self, messages):
        raise failure
    model_cls = export_model(monkeypatch, fail)
    SmolagentsInstrumentor().instrument(tracer_provider=provider)
    with pytest.raises(ValueError) as caught:
        next(model_cls().generate_stream([]))
    assert caught.value is failure
    span = exporter.get_finished_spans()[-1]
    assert span.attributes[GEN_AI_REQUEST_MODEL] == "offline-model"
    assert span.status.status_code.name == "ERROR"


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_stream_shape_and_privacy_over_localhost_otlp(tracing, monkeypatch, private, fail):
    exporter, _, provider = tracing
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", timeout=3,
    )))
    def operation(self, messages):
        yield delta("private-answer", SimpleNamespace(input_tokens=5, output_tokens=2))
        if fail:
            raise ValueError("private-error")
    model_cls = export_model(monkeypatch, operation)
    config = TraceConfig(hide_inputs=private, hide_outputs=private)
    SmolagentsInstrumentor().instrument(tracer_provider=provider, config=config)
    try:
        stream = model_cls().generate_stream([{"role": "user", "content": "private-prompt"}])
        if fail:
            with pytest.raises(ValueError, match="private-error"):
                list(stream)
        else:
            list(stream)
    finally:
        provider.force_flush()
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
    assert len(received) == 1
    request = ExportTraceServiceRequest.FromString(received[0])
    (resource,) = request.resource_spans
    (scope,) = resource.scope_spans
    (span,) = scope.spans
    attrs = {a.key: a.value for a in span.attributes}
    assert attrs[GEN_AI_SPAN_KIND].string_value == "LLM"
    assert attrs[GEN_AI_REQUEST_MODEL].string_value == "offline-model"
    assert attrs[GEN_AI_USAGE_TOTAL_TOKENS].int_value == 7
    assert span.status.code == (2 if fail else 1)
    assert len(span.events) == (1 if fail else 0)
    for secret in (b"private-prompt", b"private-answer"):
        assert (secret in received[0]) is not private
    if fail:
        # Error redaction belongs to FITracer. The CI lock includes an older
        # core without this option; current-core runs must enforce it on wire.
        hides_errors = private and hasattr(config, "hide_error_details")
        assert (b"private-error" in received[0]) is not hides_errors
