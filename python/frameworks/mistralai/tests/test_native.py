"""Native message privacy and conversation parts through the real SDK and OTLP."""

import asyncio
import json

import pytest
from fi_instrumentation import REDACTED_VALUE, TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from traceai_mistralai import MistralAIInstrumentor
import test_sdk
import test_wire
from test_sdk import (
    NATIVE,
    Mistral,
    SSE,
    USAGE,
    conversation_response,
    httpx,
    transport_response,
)
from test_wire import (
    Compression,
    JSONExporter,
    OTLPSpanExporter,
)

tracing = test_sdk.tracing
receiver = test_wire.receiver

pytestmark = pytest.mark.skipif(
    not NATIVE, reason="Native telemetry requires SDK >=3.6"
)

INPUT_TEXT = "private-input-marker"
OUTPUT_TEXT = "private-output-marker"
IMAGE = "https://example.invalid/private-image-marker"


def input_content():
    return [
        {"type": "text", "text": INPUT_TEXT},
        {"type": "image_url", "image_url": {"url": IMAGE}},
    ]


class EventSSE(SSE):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def __iter__(self):
        for event in self.events:
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def wire_spans(requests):
    return [
        span
        for _, data in requests
        for resource in data["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"hide_input_text": True},
        {"hide_output_text": True},
        {"hide_input_images": True},
        {"hide_input_text": True, "hide_output_text": True, "hide_input_images": True},
    ],
    ids=["visible", "input-text", "output-text", "input-images", "all-selective"],
)
@pytest.mark.parametrize("encoding", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize(
    "operation", ["chat", "chat-stream", "conversation-stream", "conversation-children"]
)
def test_native_selective_privacy_on_wire(
    receiver, flags, encoding, compressed, operation
):
    endpoint, requests = receiver
    exporter = (
        JSONExporter(endpoint, compressed)
        if encoding == "json"
        else OTLPSpanExporter(
            endpoint=endpoint,
            headers={},
            timeout=5,
            compression=Compression.Gzip if compressed else Compression.NoCompression,
        )
    )
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**flags))

    def respond(request):
        if operation == "conversation-stream":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=EventSSE(
                    [
                        {
                            "type": "message.output.delta",
                            "id": "entry-message",
                            "role": "assistant",
                            "content": {"type": "text", "text": OUTPUT_TEXT},
                        },
                        {
                            "type": "function.call.delta",
                            "id": "entry-function",
                            "tool_call_id": "actual-tool-call",
                            "name": "weather",
                            "arguments": '{"city":"Paris"}',
                        },
                        {"type": "conversation.response.done", "usage": USAGE},
                    ]
                ),
            )
        if operation == "conversation-children":
            body = conversation_response()
            # Real service timestamps make the native hook emit the message child.
            body["outputs"][0].update(
                id="message-child",
                created_at="2026-10-31T00:00:00Z",
                completed_at="2026-10-31T00:00:01Z",
                model="mistral-test",
            )
            return httpx.Response(200, json=body)
        return transport_response(request)

    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client = Mistral(api_key="synthetic-credential-marker", client=transport)
            if operation.startswith("conversation"):
                arguments = {
                    "model": "mistral-test",
                    "inputs": [
                        {
                            "type": "message.input",
                            "role": "user",
                            "content": input_content(),
                        }
                    ],
                }
                if operation == "conversation-stream":
                    with client.beta.conversations.start_stream(**arguments) as stream:
                        chunks = list(stream)
                    assert chunks[0].data.content.text == OUTPUT_TEXT
                    assert chunks[1].data.tool_call_id == "actual-tool-call"
                else:
                    result = client.beta.conversations.start(**arguments)
                    assert result.outputs[0].content == OUTPUT_TEXT
            else:
                arguments = {
                    "model": "mistral-test",
                    "messages": [{"role": "user", "content": input_content()}],
                }
                if operation == "chat-stream":
                    with client.chat.stream(**arguments) as stream:
                        chunks = list(stream)
                    assert chunks[0].data.choices[0].delta.content == OUTPUT_TEXT
                else:
                    result = client.chat.complete(**arguments)
                    assert result.choices[0].message.content == OUTPUT_TEXT
        provider.force_flush()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()

    spans = wire_spans(requests)
    assert len(spans) == (2 if operation == "conversation-children" else 1)
    if operation == "conversation-children":
        child, parent = spans
        assert child["parentSpanId"] == parent["spanId"]
        assert child["traceId"] == parent["traceId"]
    attrs = {a["key"]: a["value"] for a in spans[-1]["attributes"]}
    assert int(attrs["gen_ai.usage.input_tokens"]["intValue"]) == 2
    if operation == "conversation-stream":
        parts = json.loads(attrs["gen_ai.output.messages"]["stringValue"])[0]["parts"]
        assert parts == [
            {
                "type": "text",
                "content": REDACTED_VALUE
                if flags.get("hide_output_text")
                else OUTPUT_TEXT,
            },
            {
                "type": "tool_call",
                "id": "actual-tool-call",
                "name": "weather",
                "arguments": '{"city":"Paris"}',
            },
        ]
    wire = json.dumps(requests)
    for flag, marker in (
        ("hide_input_text", INPUT_TEXT),
        ("hide_output_text", OUTPUT_TEXT),
        ("hide_input_images", IMAGE),
    ):
        assert (marker in wire) is not flags.get(flag, False)
    assert "synthetic-credential-marker" not in wire
    if operation.startswith("chat"):
        # Selective text flags must not discard tool identifiers or arguments.
        assert "weather" in wire
        assert "call-1" in wire


@pytest.mark.parametrize("encoding", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("operation", ["chat", "chat-chunks", "conversation"])
@pytest.mark.parametrize(
    "policy", ["visible", "hide_input_text", "hide_inputs", "hide_input_messages"]
)
def test_native_tool_result_privacy(receiver, encoding, compressed, operation, policy):
    endpoint, requests = receiver
    exporter = (
        JSONExporter(endpoint, compressed)
        if encoding == "json"
        else OTLPSpanExporter(
            endpoint=endpoint,
            headers={},
            timeout=5,
            compression=Compression.Gzip if compressed else Compression.NoCompression,
        )
    )
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(**({} if policy == "visible" else {policy: True})),
    )
    marker = "private-tool-result-marker"
    tool_id = "input-tool-call"

    def respond(request):
        if operation == "conversation":
            body = conversation_response()
            body["outputs"] = []
            return httpx.Response(200, json=body)
        return transport_response(request)

    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client = Mistral(api_key="synthetic", client=transport)
            if operation == "conversation":
                result = client.beta.conversations.append(
                    conversation_id="conv-test",
                    inputs=[
                        {
                            "type": "function.result",
                            "tool_call_id": tool_id,
                            "result": marker,
                        }
                    ],
                )
                assert result.conversation_id == "conv-test"
            else:
                result = client.chat.complete(
                    model="mistral-test",
                    messages=[
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": [{"type": "text", "text": marker}]
                            if operation == "chat-chunks"
                            else marker,
                        },
                    ],
                )
                assert result.choices[0].message.content == OUTPUT_TEXT
        provider.force_flush()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    spans = wire_spans(requests)
    assert len(spans) == 1
    wire = json.dumps(requests)
    assert (marker in wire) == (policy == "visible")
    if operation != "conversation":
        assert OUTPUT_TEXT in wire
    if policy in ("visible", "hide_input_text"):
        attrs = {a["key"]: a["value"] for a in spans[0]["attributes"]}
        messages = json.loads(attrs["gen_ai.input.messages"]["stringValue"])
        part = messages[-1]["parts"][0]
        assert part["type"] == "tool_call_response"
        assert part["id"] == tool_id
        if policy == "hide_input_text":
            assert part["response"] == REDACTED_VALUE


@pytest.mark.parametrize("encoding", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
def test_cancelled_native_send_on_wire(receiver, encoding, compressed):
    endpoint, requests = receiver
    exporter = (
        JSONExporter(endpoint, compressed)
        if encoding == "json"
        else OTLPSpanExporter(
            endpoint=endpoint,
            headers={},
            timeout=5,
            compression=Compression.Gzip if compressed else Compression.NoCompression,
        )
    )
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_error_details=True),
    )

    async def run():
        async def respond(request):
            raise asyncio.CancelledError("private-cancellation-marker")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            client = Mistral(api_key="synthetic-key-marker", async_client=transport)
            with pytest.raises(
                asyncio.CancelledError, match="private-cancellation-marker"
            ):
                await client.chat.complete_async(
                    model="mistral-test",
                    messages=[{"role": "user", "content": "hello"}],
                )

    try:
        asyncio.run(run())
        provider.force_flush()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    spans = wire_spans(requests)
    assert len(spans) == 1
    assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR"
    assert "private-cancellation-marker" not in json.dumps(requests)
    assert "synthetic-key-marker" not in json.dumps(requests)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "variant", ["string", "text_chunk", "thinking", "image", "tool_call"]
)
def test_conversation_stream_part_conversion(tracing, variant, asynchronous):
    content, expected = {
        "string": ("chunk-content", {"type": "text", "content": "chunk-content"}),
        "text_chunk": (
            {"type": "text", "text": "chunk-content"},
            {"type": "text", "content": "chunk-content"},
        ),
        "thinking": (
            {
                "type": "thinking",
                "thinking": [{"type": "text", "text": "reasoning-content"}],
            },
            {"type": "reasoning", "content": "reasoning-content"},
        ),
        "image": (
            {"type": "image_url", "image_url": {"url": IMAGE}},
            {"type": "uri", "modality": "image", "uri": IMAGE},
        ),
        "tool_call": (
            None,
            {
                "type": "tool_call",
                "id": "actual-tool-call",
                "name": "weather",
                "arguments": '{"city":"Paris"}',
            },
        ),
    }[variant]
    event = (
        {
            "type": "function.call.delta",
            "id": "entry-function",
            "tool_call_id": "actual-tool-call",
            "name": "weather",
            "arguments": '{"city":"Paris"}',
        }
        if variant == "tool_call"
        else {
            "type": "message.output.delta",
            "id": "entry-message",
            "role": "assistant",
            "content": content,
        }
    )

    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=EventSSE(
                [event, {"type": "conversation.response.done", "usage": USAGE}]
            ),
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            client = Mistral(api_key="synthetic", async_client=transport)
            async with await client.beta.conversations.start_stream_async(
                model="mistral-test", inputs="hi"
            ) as stream:
                return [chunk async for chunk in stream]

    if asynchronous:
        chunks = asyncio.run(run())
    else:
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client = Mistral(api_key="synthetic-credential-marker", client=transport)
            with client.beta.conversations.start_stream(
                model="mistral-test", inputs="hi"
            ) as stream:
                chunks = list(stream)
    assert chunks[0].data.type == event["type"]
    if variant == "tool_call":
        assert chunks[0].data.tool_call_id == "actual-tool-call"
        assert chunks[0].data.id == "entry-function"
    elif variant == "text_chunk":
        assert chunks[0].data.content.text == "chunk-content"
    spans = tracing.get_finished_spans()
    assert len(spans) == 1
    assert json.loads(spans[0].attributes["gen_ai.output.messages"])[0]["parts"] == [
        expected
    ]


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("method", ["creation", "single", "batch"])
def test_native_privacy_at_attribute_boundaries(direction, method):
    from traceai_mistralai._native import _NativeProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = TraceConfig(**{f"hide_{direction}_text": True})
    tracer = _NativeProvider(provider, config).get_tracer("test")
    key = f"gen_ai.{direction}.messages"
    original = [
        {
            "role": "assistant",
            "parts": [{"type": "reasoning", "content": "private-reasoning"}],
        }
    ]
    attributes = {key: json.dumps(original)}
    try:
        with tracer.start_as_current_span(
            "chat", attributes=attributes if method == "creation" else None
        ) as span:
            if method == "single":
                span.set_attribute(key, attributes[key])
            elif method == "batch":
                span.set_attributes(attributes)
        result = exporter.get_finished_spans()
        assert len(result) == 1
        assert "private-reasoning" not in result[0].to_json()
        assert (
            json.loads(result[0].attributes[key])[0]["parts"][0]["content"]
            == REDACTED_VALUE
        )
        assert original[0]["parts"][0]["content"] == "private-reasoning"
        assert json.loads(attributes[key]) == original
    finally:
        provider.shutdown()
