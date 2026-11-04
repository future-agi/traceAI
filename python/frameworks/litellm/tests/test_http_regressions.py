"""Independent local-HTTP contract. Run only in E2B, with outbound network disabled."""

import gc
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import litellm
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_litellm import LiteLLMInstrumentor

MODEL = "gpt-4o-mini"
RETURNED_MODEL = "gpt-4o-mini-2024-07-18"
USAGE = {
    "prompt_tokens": 9,
    "completion_tokens": 4,
    "total_tokens": 13,
    "prompt_tokens_details": {"cached_tokens": 3, "audio_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 2, "audio_tokens": 1},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, request))
        if "error" in self.path:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "error": {
                            "message": "synthetic-private-error",
                            "type": "invalid_request_error",
                        }
                    }
                ).encode()
            )
            return
        if self.path.endswith("/embeddings"):
            response = {
                "object": "list",
                "model": "text-embedding-ada-002",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                "usage": {"prompt_tokens": 9, "total_tokens": 9},
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
            return
        if self.path.endswith("/responses"):
            response = {
                "id": "resp_synthetic",
                "object": "response",
                "instructions": request.get("instructions"),
                "metadata": {"private": "synthetic-echoed-metadata"},
                "created_at": 1,
                "status": "completed",
                "model": RETURNED_MODEL,
                "output": [
                    {
                        "id": "msg_synthetic",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "synthetic-output",
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "usage": {
                    "input_tokens": 9,
                    "output_tokens": 4,
                    "total_tokens": 13,
                    "input_tokens_details": {"cached_tokens": 3},
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
            }
            tool = {
                "type": "function_call",
                "id": "fc_synthetic",
                "call_id": "call_synthetic",
                "name": "lookup",
                "arguments": '{"x":2}',
                "status": "completed",
            }
            if request.get("tools"):
                response["output"].append(tool)
            self.send_response(200)
            if request.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                message = response["output"][0]
                events = [
                    {
                        "type": "response.created",
                        "response": {
                            **response,
                            "status": "in_progress",
                            "output": [],
                            "usage": None,
                        },
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**message, "content": []},
                    },
                    {
                        "type": "response.content_part.added",
                        "output_index": 0,
                        "content_index": 0,
                        "item_id": message["id"],
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                ]
                for delta in ("synthetic-", "output"):
                    events.append(
                        {
                            "type": "response.output_text.delta",
                            "output_index": 0,
                            "content_index": 0,
                            "item_id": message["id"],
                            "delta": delta,
                        }
                    )
                events.extend(
                    [
                        {
                            "type": "response.output_text.done",
                            "output_index": 0,
                            "content_index": 0,
                            "item_id": message["id"],
                            "text": "synthetic-output",
                        },
                        {
                            "type": "response.content_part.done",
                            "output_index": 0,
                            "content_index": 0,
                            "item_id": message["id"],
                            "part": message["content"][0],
                        },
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": message,
                        },
                    ]
                )
                if request.get("tools"):
                    events.append(
                        {
                            "type": "response.output_item.added",
                            "output_index": 1,
                            "item": {**tool, "arguments": ""},
                        }
                    )
                    for delta in ('{"x":', "2}"):
                        events.append(
                            {
                                "type": "response.function_call_arguments.delta",
                                "output_index": 1,
                                "item_id": tool["id"],
                                "delta": delta,
                            }
                        )
                    events.extend(
                        [
                            {
                                "type": "response.function_call_arguments.done",
                                "output_index": 1,
                                "item_id": tool["id"],
                                "arguments": tool["arguments"],
                            },
                            {
                                "type": "response.output_item.done",
                                "output_index": 1,
                                "item": tool,
                            },
                        ]
                    )
                events.append({"type": "response.completed", "response": response})
                for seq, event in enumerate(events):
                    event["sequence_number"] = seq
                    self.wfile.write(
                        (
                            "event: "
                            + event["type"]
                            + "\ndata: "
                            + json.dumps(event)
                            + "\n\n"
                        ).encode()
                    )
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
            return
        tools = bool(request.get("tools"))
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if tools:
                deltas = [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_synthetic",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"x":'},
                            }
                        ],
                    },
                    {"tool_calls": [{"index": 0, "function": {"arguments": "2}"}}]},
                ]
            else:
                deltas = [
                    {"role": "assistant", "content": "synthetic-"},
                    {"content": "output"},
                ]
            chunks = [
                {
                    "id": "chatcmpl_synthetic",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": RETURNED_MODEL,
                    "choices": [{"index": 0, "delta": d, "finish_reason": None}],
                }
                for d in deltas
            ]
            chunks += [
                {
                    "id": "chatcmpl_synthetic",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": RETURNED_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls" if tools else "stop",
                        }
                    ],
                    "usage": USAGE,
                }
            ]
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            message = {"role": "assistant", "content": "synthetic-output"}
            if tools:
                message["tool_calls"] = [
                    {
                        "id": "call_synthetic",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"x":2}'},
                    }
                ]
            response = {
                "id": "chatcmpl_synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": RETURNED_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if tools else "stop",
                    }
                ],
                "usage": USAGE,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())


@pytest.fixture
def server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, "http://127.0.0.1:%d/v1" % server.server_port
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def capture():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    inst = LiteLLMInstrumentor()
    inst.instrument(tracer_provider=provider, config=TraceConfig())
    yield exporter, provider
    inst.uninstrument()
    provider.shutdown()


async def call(base, asynchronous=False, streaming=False, **kwargs):
    params = dict(
        model=MODEL,
        messages=[
            {"role": "system", "content": "synthetic-system"},
            {"role": "user", "content": "synthetic-input"},
        ],
        api_base=base,
        api_key="synthetic-provider-key",
        num_retries=0,
        stream=streaming,
        **kwargs,
    )
    if streaming:
        params["stream_options"] = {"include_usage": True}
    result = (
        await litellm.acompletion(**params)
        if asynchronous
        else litellm.completion(**params)
    )
    if streaming:
        if asynchronous:
            return [part async for part in result]
        return list(result)
    return result


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_real_http_core_contract(server, capture, asynchronous, streaming):
    http, base = server
    exporter, provider = capture
    with provider.get_tracer("review").start_as_current_span("application") as parent:
        result = await call(base, asynchronous, streaming)
        assert result
        assert trace.get_current_span() is parent
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.status.status_code is StatusCode.OK
        attrs = dict(span.attributes)
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.request.model"] == MODEL
        assert "synthetic-input" in attrs["input.value"]
        assert "synthetic-system" in attrs["input.value"]
        assert attrs["output.value"] == "synthetic-output"
        assert attrs["gen_ai.usage.input_tokens"] == 9
        assert attrs["gen_ai.usage.output_tokens"] == 4
        assert attrs["gen_ai.usage.total_tokens"] == 13
        assert "synthetic-provider-key" not in span.to_json()
    assert len(http.requests) == 1
    assert len(exporter.get_finished_spans()) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_provider_and_response_model(server, capture, streaming, asynchronous):
    _, base = server
    exporter, _ = capture
    result = await call(
        base,
        asynchronous=asynchronous,
        streaming=streaming,
        custom_llm_provider="openai",
    )
    response_model = result[-1].model if streaming else result.model
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert attrs.get("gen_ai.provider.name") == "openai"
    assert attrs.get("gen_ai.response.model") == response_model


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_detailed_usage(server, capture, streaming, asynchronous):
    _, base = server
    exporter, _ = capture
    result = await call(base, asynchronous=asynchronous, streaming=streaming)
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    reported_usage = (result[-1] if streaming else result).usage.model_dump()
    if reported_usage.get("cost") is not None:
        assert attrs["gen_ai.cost.total"] == reported_usage["cost"]
    from importlib.metadata import version

    if streaming and version("litellm") == "1.43.0":
        # The floor SDK strips nested usage before yielding its final chunk.
        # Do not invent the fields it never exposes to an instrumentor.
        assert not reported_usage.get("prompt_tokens_details")
        assert not reported_usage.get("completion_tokens_details")
        assert "gen_ai.usage.input_tokens.cache_read" not in attrs
        assert "gen_ai.usage.output_tokens.reasoning" not in attrs
    else:
        assert attrs["gen_ai.usage.input_tokens.cache_read"] == 3
        assert attrs["gen_ai.usage.output_tokens.reasoning"] == 2
        assert attrs["gen_ai.usage.input_tokens.audio"] == 0
        assert attrs["gen_ai.usage.output_tokens.audio"] == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_real_http_error_privacy(
    server, capture, monkeypatch, asynchronous, streaming
):
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    _, base = server
    exporter, provider = capture
    inst = LiteLLMInstrumentor()
    inst.uninstrument()
    inst.instrument(tracer_provider=provider, config=TraceConfig())
    with pytest.raises(Exception):
        await call(base + "/error", asynchronous, streaming)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert "synthetic-private-error" not in spans[0].to_json()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_stream_tool_semantics(server, capture, asynchronous):
    _, base = server
    exporter, _ = capture
    tool = {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
        },
    }
    await call(base, asynchronous=asynchronous, streaming=True, tools=[tool])
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert (
        attrs.get(
            "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"
        )
        == "lookup"
    )
    assert (
        attrs.get(
            "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"
        )
        == '{"x":2}'
    )


@pytest.mark.skipif(
    not hasattr(litellm, "responses"),
    reason="Responses API did not exist at declared floor",
)
def test_responses_endpoint_is_traced(server, capture):
    http, base = server
    exporter, _ = capture
    result = litellm.responses(
        model="openai/" + MODEL,
        input="synthetic-input",
        api_base=base,
        api_key="synthetic-provider-key",
        num_retries=0,
    )
    assert result.output[0].content[0].text == "synthetic-output"
    assert http.requests[-1][0].endswith("/responses")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, "Successful real Responses request exported no span"


def test_unread_stream_is_eventually_finalized(server, capture):
    _, base = server
    exporter, _ = capture
    stream = litellm.completion(
        model=MODEL,
        messages=[{"role": "user", "content": "synthetic-input"}],
        api_base=base,
        api_key="synthetic-provider-key",
        num_retries=0,
        stream=True,
    )
    span = stream.span
    del stream
    gc.collect()
    assert len(exporter.get_finished_spans()) == 1, (
        "Discarded unread stream left a recording span open"
    )
    assert not span.is_recording()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_embedding_provider_and_model(server, capture, asynchronous):
    _, base = server
    exporter, _ = capture
    params = dict(
        model="openai/text-embedding-ada-002",
        input=["hello"],
        api_base=base,
        api_key="synthetic-provider-key",
        custom_llm_provider="openai",
    )
    result = (
        await litellm.aembedding(**params)
        if asynchronous
        else litellm.embedding(**params)
    )
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.response.model"] == result.model
    assert "synthetic-provider-key" not in json.dumps(dict(attrs))


@pytest.mark.skipif(
    not hasattr(litellm, "responses"), reason="Responses API absent at floor"
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "flag",
    [
        None,
        "hide_inputs",
        "hide_input_text",
        "hide_input_messages",
        "hide_outputs",
        "hide_output_text",
        "hide_output_messages",
        "hide_input_images",
    ],
)
@pytest.mark.asyncio
async def test_responses_real_sdk(server, capture, asynchronous, streaming, flag):
    http, base = server
    exporter, provider = capture
    inst = LiteLLMInstrumentor()
    inst.uninstrument()
    inst.instrument(
        tracer_provider=provider, config=TraceConfig(**({flag: True} if flag else {}))
    )
    image = "data:image/png;base64,c3ludGhldGlj"
    params = dict(
        model="openai/" + MODEL,
        instructions="synthetic-instructions",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "synthetic-input"},
                    {"type": "input_image", "image_url": image},
                ],
            }
        ],
        tools=[
            {"type": "function", "name": "lookup", "parameters": {"type": "object"}}
        ],
        api_base=base,
        api_key="synthetic-provider-key",
        stream=streaming,
        num_retries=0,
    )
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        result = (
            await litellm.aresponses(**params)
            if asynchronous
            else litellm.responses(**params)
        )
        if streaming:
            assert not exporter.get_finished_spans()
            if asynchronous:
                chunks = [part async for part in result]
                await result.aclose()
            else:
                chunks = list(result)
                result.close()
            assert chunks[-1].type == "response.completed"
        else:
            assert result.output[0].content[0].text == "synthetic-output"
        assert trace.get_current_span() is parent
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.status.status_code is StatusCode.OK
        attrs = dict(span.attributes)
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.response.model"] == RETURNED_MODEL
        assert attrs["gen_ai.usage.input_tokens"] == 9
        assert attrs["gen_ai.usage.output_tokens"] == 4
        assert attrs["gen_ai.usage.input_tokens.cache_read"] == 3
        assert attrs["gen_ai.usage.output_tokens.reasoning"] == 2
        assert "synthetic-provider-key" not in span.to_json()
        assert "synthetic-echoed-metadata" not in span.to_json()
        if flag in ("hide_inputs", "hide_input_text", "hide_input_messages"):
            assert "synthetic-input" not in span.to_json()
            assert "synthetic-instructions" not in span.to_json()
        else:
            assert "synthetic-input" in attrs["input.value"]
            assert "synthetic-instructions" in attrs["input.value"]
        if flag in ("hide_outputs", "hide_output_text", "hide_output_messages"):
            assert "synthetic-output" not in span.to_json()
            assert '{"x":2}' not in json.dumps(attrs).replace('\\"', '"')
        else:
            prefix = "gen_ai.output.messages.1.message.tool_calls.0.tool_call."
            assert attrs[prefix + "id"] == "call_synthetic"
            assert attrs[prefix + "function.name"] == "lookup"
            assert attrs[prefix + "function.arguments"] == '{"x":2}'
            assert (
                attrs[
                    "gen_ai.output.messages.0.message.contents.0.message_content.text"
                ]
                == "synthetic-output"
            )
        if flag == "hide_input_images":
            assert image not in span.to_json()
    assert len(http.requests) == 1
    assert http.requests[0][1]["input"][0]["content"][1]["image_url"] == image


@pytest.mark.skipif(
    not hasattr(litellm, "responses"), reason="Responses API absent at floor"
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_responses_suppression_restoration_and_errors(
    server, capture, asynchronous, monkeypatch
):
    _, base = server
    exporter, provider = capture
    inst = LiteLLMInstrumentor()
    name = "aresponses" if asynchronous else "responses"
    original = inst.original_litellm_funcs[name]
    params = dict(
        model="openai/" + MODEL,
        input="synthetic-input",
        api_base=base,
        api_key="synthetic-provider-key",
        num_retries=0,
    )
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        result = (
            await litellm.aresponses(**params)
            if asynchronous
            else litellm.responses(**params)
        )
        assert result.output[0].content[0].text == "synthetic-output"
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()
    inst.uninstrument()
    assert getattr(litellm, name) is original
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    inst.instrument(tracer_provider=provider, config=TraceConfig())
    params["api_base"] = base + "/error"
    with pytest.raises(Exception):
        if asynchronous:
            await litellm.aresponses(**params)
        else:
            litellm.responses(**params)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert "synthetic-private-error" not in spans[0].to_json()
