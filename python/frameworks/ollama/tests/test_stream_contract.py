"""Real Ollama clients, synthetic HTTP and serialized OTLP; no model service."""
import asyncio
import inspect
import json

import httpx
import ollama
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_ollama import OllamaInstrumentor
from traceai_ollama._utils import _to_dict


@pytest.fixture
def tracing():
    aliases = {name: getattr(ollama, name) for name in ("chat", "generate", "embed", "embeddings") if hasattr(ollama, name)}
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OllamaInstrumentor()

    def start(**privacy):
        exporter.clear()
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
        return exporter

    yield start
    instrumentor.uninstrument()
    # Also isolate the baseline reproduction, whose uninstrument leaves aliases patched.
    for name, method in aliases.items():
        setattr(ollama, name, method)
    provider.shutdown()


def chunks(endpoint):
    body = [
        {"model": "offline-model", "done": False},
        {"model": "offline-model", "done": False},
        {"model": "offline-model", "done": True, "prompt_eval_count": 5, "eval_count": 2, "total_duration": 123},
    ]
    for index, chunk in enumerate(body):
        text = ("hello ", "world", "")[index]
        thinking = ("consider ", "carefully", "")[index]
        if endpoint == "chat":
            chunk["message"] = {"role": "assistant", "content": text, "thinking": thinking}
            if index < 2:
                chunk["message"]["tool_calls"] = [{"function": {"name": "weather", "arguments": {"city": ("Rome", "Paris")[index]}}}]
        else:
            chunk.update(response=text, thinking=thinking)
    return body


def transport(endpoint, streaming=True):
    body = chunks(endpoint)
    if not streaming:
        body = body[0]
        body.update(done=True, prompt_eval_count=5, eval_count=2)
    return httpx.MockTransport(lambda request: httpx.Response(
        200, content="".join(json.dumps(chunk) + "\n" for chunk in body) if streaming else json.dumps(body),
        headers={"content-type": "application/x-ndjson" if streaming else "application/json"},
    ))


def parameters(endpoint, stream=True):
    return dict(model="offline-model", stream=stream, **({"messages": [{"role": "user", "content": "hello"}]} if endpoint == "chat" else {"prompt": "hello"}))


def wire_attributes(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    encoded = encode_spans(spans).SerializeToString()
    decoded = ExportTraceServiceRequest.FromString(encoded)
    span = decoded.resource_spans[0].scope_spans[0].spans[0]
    return {item.key: getattr(item.value, item.value.WhichOneof("value")) for item in span.attributes}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "generate"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_sdk_return_contract(tracing, endpoint, asynchronous, streaming):
    exporter = tracing()
    cls = ollama.AsyncClient if asynchronous else ollama.Client
    client = cls(host="http://offline.invalid", transport=transport(endpoint, streaming), trust_env=False)
    try:
        result = getattr(client, endpoint)(**parameters(endpoint, streaming))
        if asynchronous:
            result = await result
        if streaming:
            # An extra coroutine here breaks the SDK's documented single-await contract.
            if inspect.iscoroutine(result):
                result.close()
                pytest.fail("await client call returned a coroutine, not an async iterator")
            received = [item async for item in result] if asynchronous else list(result)
            assert len(received) == 3
        else:
            received = [result]
        attrs = wire_attributes(exporter)
        assert attrs["gen_ai.usage.input_tokens"] == 5
        assert attrs["gen_ai.usage.output_tokens"] == 2
        assert attrs["gen_ai.usage.total_tokens"] == 7
        assert attrs["gen_ai.provider.name"] == "ollama"
        assert attrs["gen_ai.output.messages.0.message.content"] == ("hello world" if streaming else "hello ")
        # Old SDKs return dicts; current SDKs return Pydantic models. Neither is mutated.
        first = _to_dict(received[0])
        first_output = first["message"] if endpoint == "chat" else first
        assert first_output["content" if endpoint == "chat" else "response"] == "hello "
        if "thinking" in first_output:
            output = json.loads(attrs["output.value"])
            assert (output["message"] if endpoint == "chat" else output)["thinking"] == ("consider carefully" if streaming else "consider ")
        if endpoint == "chat":
            prefix = "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function"
            assert attrs[prefix + ".name"] == "weather"
            assert json.loads(attrs[prefix + ".arguments"]) == {"city": "Rome"}
            if streaming:
                assert json.loads(attrs[prefix.replace("tool_calls.0", "tool_calls.1") + ".arguments"]) == {"city": "Paris"}
    finally:
        if asynchronous:
            await client._client.aclose()
        else:
            client._client.close()


@pytest.mark.parametrize("endpoint", ["chat", "generate", "embed", "embeddings"])
def test_alias_restoration_and_reinstrumentation(tracing, endpoint):
    if not hasattr(ollama, endpoint):
        pytest.skip("Endpoint unavailable at the SDK floor")
    original = getattr(ollama, endpoint)
    original_class = getattr(ollama.Client, endpoint)
    original_async = getattr(ollama.AsyncClient, endpoint)
    for _ in range(2):
        tracing()
        assert getattr(ollama, endpoint) is not original
        OllamaInstrumentor().uninstrument()
        assert getattr(ollama, endpoint) is original
        assert getattr(ollama.Client, endpoint) is original_class
        assert getattr(ollama.AsyncClient, endpoint) is original_async


@pytest.mark.parametrize("endpoint", ["chat", "generate", "embed", "embeddings"])
def test_module_helpers_export_once_across_reinstrumentation(tracing, monkeypatch, endpoint):
    if not hasattr(ollama, endpoint):
        pytest.skip("Endpoint unavailable at the SDK floor")
    bound_client = getattr(ollama, endpoint).__self__
    if endpoint in ("chat", "generate"):
        mock_transport = transport(endpoint)
        params = parameters(endpoint)
    else:
        body = {"embedding": [0.1], "embeddings": [[0.1]]}
        mock_transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        params = {"model": "offline-model", "input" if endpoint == "embed" else "prompt": "hello"}
    with httpx.Client(transport=mock_transport, base_url="http://offline.invalid", trust_env=False) as client:
        monkeypatch.setattr(bound_client, "_client", client)
        for _ in range(2):
            exporter = tracing()
            response = getattr(ollama, endpoint)(**params)
            if endpoint in ("chat", "generate"):
                assert len(list(response)) == 3
            assert len(exporter.get_finished_spans()) == 1
            OllamaInstrumentor().uninstrument()
            response = getattr(ollama, endpoint)(**params)
            if endpoint in ("chat", "generate"):
                list(response)
            assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "generate"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("consume_first", [False, True])
async def test_close_ends_span_even_before_iteration(tracing, endpoint, asynchronous, consume_first):
    exporter = tracing()
    cls = ollama.AsyncClient if asynchronous else ollama.Client
    client = cls(host="http://offline.invalid", transport=transport(endpoint), trust_env=False)
    try:
        stream = getattr(client, endpoint)(**parameters(endpoint))
        if asynchronous:
            stream = await stream
            if consume_first:
                await stream.__anext__()
            await stream.aclose()
            await stream.aclose()
        else:
            if consume_first:
                next(stream)
            stream.close()
            stream.close()
        assert len(exporter.get_finished_spans()) == 1
        assert exporter.get_finished_spans()[0].status.status_code.name == "UNSET"
    finally:
        if asynchronous:
            await client._client.aclose()
        else:
            client._client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "generate"])
async def test_cancellation_propagates_and_ends_span(tracing, endpoint):
    exporter = tracing()
    waiting = asyncio.Event()
    closed = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (json.dumps(chunks(endpoint)[0]) + "\n").encode()
            waiting.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    client = ollama.AsyncClient(host="http://offline.invalid", transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body())), trust_env=False)
    try:
        stream = await getattr(client, endpoint)(**parameters(endpoint))
        await stream.__anext__()
        task = asyncio.create_task(stream.__anext__())
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert len(exporter.get_finished_spans()) == 1
        assert exporter.get_finished_spans()[0].status.status_code.name != "OK"
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "generate"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("flag", ["hide_outputs", "hide_output_messages", "hide_output_text"])
async def test_output_privacy_on_otlp_wire(tracing, endpoint, asynchronous, streaming, flag):
    exporter = tracing(**{flag: True})
    cls = ollama.AsyncClient if asynchronous else ollama.Client
    client = cls(host="http://offline.invalid", transport=transport(endpoint, streaming), trust_env=False)
    try:
        result = getattr(client, endpoint)(**parameters(endpoint, streaming))
        if asynchronous:
            result = await result
        if streaming:
            received = [item async for item in result] if asynchronous else list(result)
        else:
            received = [result]
        data = _to_dict(received[0])
        message = data["message"] if endpoint == "chat" else data
        assert message["content" if endpoint == "chat" else "response"] == "hello "
        attrs = wire_attributes(exporter)
        # The input contains "hello" as well; inspect output fields only.
        output = json.dumps({key: value for key, value in attrs.items() if key == "output.value" or key.startswith("gen_ai.output.")})
        assert "hello" not in output
        assert "consider" not in output
        if flag != "hide_output_text":
            assert "Rome" not in output
            assert "weather" not in output
        assert attrs["gen_ai.usage.total_tokens"] == 7
    finally:
        if asynchronous:
            await client._client.aclose()
        else:
            client._client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "generate"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_partial_http_stream_closes_and_restores_context(tracing, endpoint, asynchronous, failure):
    from opentelemetry import trace

    exporter = tracing(hide_error_details=True)
    closed = []
    active = []

    class Body(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            active.append(trace.get_current_span().get_span_context().span_id)
            yield (json.dumps(chunks(endpoint)[0]) + "\n").encode()
            if failure:
                raise RuntimeError("private-stream-error")
            yield (json.dumps(chunks(endpoint)[1]) + "\n").encode()

        async def __aiter__(self):
            for item in self:
                yield item

        def close(self):
            closed.append(True)

        async def aclose(self):
            self.close()

    cls = ollama.AsyncClient if asynchronous else ollama.Client
    client = cls(host="http://offline.invalid", transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body())), trust_env=False)
    before = trace.get_current_span()
    try:
        stream = getattr(client, endpoint)(**parameters(endpoint))
        if asynchronous:
            stream = await stream
            await stream.__anext__()
        else:
            next(stream)
        assert trace.get_current_span() is before
        if failure:
            with pytest.raises(RuntimeError, match="private-stream-error"):
                if asynchronous:
                    await stream.__anext__()
                else:
                    next(stream)
        if asynchronous:
            await stream.aclose()
        else:
            stream.close()
        assert len(closed) == 1
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert active == [spans[0].context.span_id]
        assert spans[0].status.status_code.name == ("ERROR" if failure else "UNSET")
        assert b"private-stream-error" not in encode_spans(spans).SerializeToString()
        attrs = wire_attributes(exporter)
        assert attrs["gen_ai.output.messages.0.message.content"] == "hello "
        assert trace.get_current_span() is before
    finally:
        if asynchronous:
            await client._client.aclose()
        else:
            client._client.close()


@pytest.mark.parametrize("endpoint", ["chat", "generate"])
def test_bounded_preview_keeps_final_usage(tracing, endpoint):
    from traceai_ollama._stream import _MAX_OUTPUT_CHARACTERS

    exporter = tracing()
    first, _, last = chunks(endpoint)
    message = first["message"] if endpoint == "chat" else first
    key = "content" if endpoint == "chat" else "response"
    message[key] = "x" * 1024
    message.pop("thinking", None)
    message.pop("tool_calls", None)
    line = json.dumps(first) + "\n"
    data = line * 256 + json.dumps(last) + "\n"
    client = ollama.Client(host="http://offline.invalid", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data)), trust_env=False)
    try:
        result = list(getattr(client, endpoint)(**parameters(endpoint)))
        assert len(result) == 257
        assert (_to_dict(result[0])["message"] if endpoint == "chat" else _to_dict(result[0]))[key] == "x" * 1024
        attrs = wire_attributes(exporter)
        assert attrs["ollama.output_truncated"] is True
        assert len(attrs["gen_ai.output.messages.0.message.content"]) == _MAX_OUTPUT_CHARACTERS
        assert attrs["gen_ai.usage.total_tokens"] == 7
        assert attrs["ollama.total_duration_ns"] == 123
    finally:
        client._client.close()


def test_hidden_stream_does_not_retain_payload():
    from traceai_ollama._stream import _ResponseAccumulator

    accumulator = _ResponseAccumulator(TraceConfig(hide_outputs=True))
    for chunk in chunks("chat"):
        accumulator.add(chunk)
    assert accumulator._content == []
    assert accumulator._thinking == []
    assert accumulator._tools == []
    assert accumulator.response()["eval_count"] == 2


def test_tool_budget_preserves_valid_json_and_detaches_arguments():
    from traceai_ollama._stream import _MAX_TOOL_CALLS, _ResponseAccumulator

    accumulator = _ResponseAccumulator(TraceConfig())
    chunk = chunks("chat")[0]
    for _ in range(1000):
        accumulator.add(chunk)
    assert accumulator.truncated
    tools = accumulator.response()["message"]["tool_calls"]
    assert len(tools) == _MAX_TOOL_CALLS
    chunk["message"]["tool_calls"][0]["function"]["arguments"]["city"] = "changed"
    assert tools[0]["function"]["arguments"]["city"] == "Rome"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("endpoint", ["chat", "generate"])
async def test_positional_stream_argument(tracing, asynchronous, endpoint):
    exporter = tracing()
    cls = ollama.AsyncClient if asynchronous else ollama.Client
    # SDK releases have different signatures; bind the public signature first.
    signature = inspect.signature(getattr(cls, endpoint))
    bound = signature.bind(None, **parameters(endpoint))
    bound.apply_defaults()
    client = cls(host="http://offline.invalid", transport=transport(endpoint), trust_env=False)
    try:
        result = getattr(client, endpoint)(*bound.args[1:], **bound.kwargs)
        if asynchronous:
            result = await result
            assert len([item async for item in result]) == 3
        else:
            assert len(list(result)) == 3
        assert wire_attributes(exporter)["gen_ai.usage.total_tokens"] == 7
    finally:
        if asynchronous:
            await client._client.aclose()
        else:
            client._client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_suppression_preserves_native_stream(tracing, asynchronous):
    import opentelemetry.context as context

    exporter = tracing()
    cls = ollama.AsyncClient if asynchronous else ollama.Client
    client = cls(host="http://offline.invalid", transport=transport("chat"), trust_env=False)
    token = context.attach(context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        stream = client.chat(**parameters("chat"))
        if asynchronous:
            stream = await stream
            assert len([chunk async for chunk in stream]) == 3
        else:
            assert len(list(stream)) == 3
        assert exporter.get_finished_spans() == ()
    finally:
        context.detach(token)
        if asynchronous:
            await client._client.aclose()
        else:
            client._client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_empty_stream_finishes_once(tracing, asynchronous):
    exporter = tracing()
    cls = ollama.AsyncClient if asynchronous else ollama.Client
    client = cls(host="http://offline.invalid", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"")), trust_env=False)
    try:
        stream = client.chat(**parameters("chat"))
        if asynchronous:
            stream = await stream
            assert [chunk async for chunk in stream] == []
            await stream.aclose()
        else:
            assert list(stream) == []
            stream.close()
        assert len(exporter.get_finished_spans()) == 1
        assert exporter.get_finished_spans()[0].status.status_code.name == "OK"
    finally:
        if asynchronous:
            await client._client.aclose()
        else:
            client._client.close()


def test_optional_protect_restored(tracing, monkeypatch):
    import sys
    from types import ModuleType
    import traceai_ollama

    class Protect:
        def protect(self):
            return "protected"

    original = Protect.protect
    fi_module = ModuleType("fi")
    eval_module = ModuleType("fi.evals")
    eval_module.Protect = Protect
    fi_module.evals = eval_module
    monkeypatch.setitem(sys.modules, "fi", fi_module)
    monkeypatch.setitem(sys.modules, "fi.evals", eval_module)
    monkeypatch.setattr(traceai_ollama, "Protect", Protect)
    tracing()
    assert Protect.protect is not original
    OllamaInstrumentor().uninstrument()
    assert Protect.protect is original
