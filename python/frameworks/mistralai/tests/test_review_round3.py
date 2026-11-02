"""Independent real-SDK contracts; synthetic credentials and no vendor calls."""

import asyncio
import importlib
import json
from contextlib import contextmanager
from importlib.metadata import version

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_mistralai import MistralAIInstrumentor

NATIVE = version("mistralai").startswith("3.")
Mistral = importlib.import_module("mistralai.client" if NATIVE else "mistralai").Mistral
models = importlib.import_module(
    "mistralai.client.models" if NATIVE else "mistralai.models"
)
httpx = importlib.import_module("httpx2" if NATIVE else "httpx")
INPUT = "synthetic-input-content-marker"
OUTPUT = "synthetic-output-content-marker"
IMAGE = "https://example.invalid/synthetic-image-content-marker"


@contextmanager
def tracing(**flags):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**flags))
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def response(content=OUTPUT):
    return {
        "id": "synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": "mistral-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def call(asynchronous, messages, body, captured):
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    if asynchronous:

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as transport:
                client = Mistral(api_key="synthetic-key", async_client=transport)
                return await client.chat.complete_async(
                    model="mistral-test", messages=messages
                )

        return asyncio.run(run())
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = Mistral(api_key="synthetic-key", client=transport)
        return client.chat.complete(model="mistral-test", messages=messages)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "shape",
    ["dict", "model", "typed-chunk", "image-dict", "image-model", "tool-history"],
)
@pytest.mark.parametrize("hide", [False, True])
def test_supported_input_shapes_preserve_capture(asynchronous, shape, hide):
    marker = INPUT
    if shape == "dict":
        messages = [{"role": "user", "content": INPUT}]
    elif shape == "model":
        messages = [models.UserMessage(content=INPUT)]
    elif shape == "typed-chunk":
        messages = [{"role": "user", "content": [models.TextChunk(text=INPUT)]}]
    elif shape == "image-dict":
        marker = IMAGE
        messages = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": IMAGE}}],
            }
        ]
    elif shape == "image-model":
        if NATIVE:
            pytest.skip("Legacy typed image model")
        marker = IMAGE
        messages = [models.UserMessage(content=[models.ImageURLChunk(image_url=IMAGE)])]
    else:
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-synthetic",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": json.dumps({"query": INPUT}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-synthetic",
                "content": "tool-result",
            },
        ]
    with tracing(**({"hide_inputs": True} if hide else {})) as exporter:
        captured = []
        result = call(asynchronous, messages, response(), captured)
        assert result.choices[0].message.content == OUTPUT
        assert marker in json.dumps(captured)
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.OK
        assert (marker in span.to_json()) == (not hide), dict(span.attributes)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("shape", ["string", "chunks"])
@pytest.mark.parametrize("hide", [False, True])
def test_supported_output_shapes_preserve_capture(asynchronous, shape, hide):
    if shape == "chunks" and version("mistralai") == "1.1.0":
        pytest.skip("SDK 1.1 response content accepts strings only")
    content = OUTPUT if shape == "string" else [{"type": "text", "text": OUTPUT}]
    with tracing(**({"hide_output_text": True} if hide else {})) as exporter:
        result = call(
            asynchronous, [{"role": "user", "content": INPUT}], response(content), []
        )
        assert OUTPUT in result.model_dump_json()
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.OK
        assert (OUTPUT in span.to_json()) == (not hide), dict(span.attributes)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("policy", ["argument", "environment", "default", "visible"])
def test_oversized_input_image_policy(asynchronous, policy, monkeypatch):
    image = "data:image/png;base64," + "YWJj" * 9000
    flags = {}
    if policy == "environment":
        monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", "32")
    elif policy == "argument":
        flags["base64_image_max_length"] = 32
    elif policy == "visible":
        flags["base64_image_max_length"] = 40000
    with tracing(**flags) as exporter:
        captured = []
        result = call(
            asynchronous,
            [{"role": "user", "content": [{"type": "image_url", "image_url": image}]}],
            response(),
            captured,
        )
        assert result.choices[0].message.content == OUTPUT
        assert image in json.dumps(captured)
        (span,) = exporter.get_finished_spans()
        captured_image = image in span.to_json()
        assert captured_image == (
            policy == "visible"
        ), "Oversized input image capture does not follow the configured policy"


@pytest.mark.skipif(
    not hasattr(
        importlib.import_module(
            "mistralai.client.chat" if NATIVE else "mistralai.chat"
        ).Chat,
        "parse",
    ),
    reason="No structured output API on this SDK",
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_structured_parse_status(asynchronous, valid):
    from pydantic import BaseModel

    class Answer(BaseModel):
        city: str

    body = response('{"city":"Paris"}' if valid else "synthetic-invalid-json")

    def respond(request):
        return httpx.Response(200, json=body)

    def operation(client):
        method = client.chat.parse_async if asynchronous else client.chat.parse
        return method(
            response_format=Answer,
            model="mistral-test",
            messages=[{"role": "user", "content": "city"}],
        )

    with tracing() as exporter:
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    client = Mistral(api_key="synthetic", async_client=transport)
                    if valid:
                        result = await operation(client)
                        assert result.choices[0].message.parsed.city == "Paris"
                    else:
                        with pytest.raises(json.JSONDecodeError):
                            await operation(client)

            asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = Mistral(api_key="synthetic", client=transport)
                if valid:
                    assert operation(client).choices[0].message.parsed.city == "Paris"
                else:
                    with pytest.raises(json.JSONDecodeError):
                        operation(client)
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == (StatusCode.OK if valid else StatusCode.ERROR)
        if not valid:
            assert len(span.events) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("shape", ["string", "chunks"])
@pytest.mark.parametrize("hide", [False, True])
def test_stream_content_shapes(asynchronous, shape, hide):
    if shape == "chunks" and version("mistralai") == "1.1.0":
        pytest.skip("SDK 1.1 output deltas accept strings only")
    content = OUTPUT if shape == "string" else [{"type": "text", "text": OUTPUT}]
    event = {
        "id": "synthetic",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mistral-test",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }

    class Stream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            yield ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()

        async def __aiter__(self):
            for b in self:
                yield b

        def close(self):
            pass

        async def aclose(self):
            pass

    def respond(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        )

    with tracing(**({"hide_output_text": True} if hide else {})) as exporter:
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    client = Mistral(api_key="synthetic", async_client=transport)
                    async with await client.chat.stream_async(
                        model="mistral-test",
                        messages=[{"role": "user", "content": INPUT}],
                    ) as stream:
                        return [chunk async for chunk in stream]

            chunks = asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = Mistral(api_key="synthetic", client=transport)
                with client.chat.stream(
                    model="mistral-test", messages=[{"role": "user", "content": INPUT}]
                ) as stream:
                    chunks = list(stream)
        assert OUTPUT in chunks[0].model_dump_json()
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.OK
        assert span.attributes["gen_ai.usage.input_tokens"] == 2
        assert (OUTPUT in span.to_json()) == (not hide), dict(span.attributes)
