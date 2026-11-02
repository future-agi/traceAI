"""Structured tool images obey the same policy as native user-message images."""

import copy
import json

import pytest
from fi_instrumentation import REDACTED_VALUE, TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from traceai_mistralai import MistralAIInstrumentor
from test_sdk import NATIVE, Mistral, httpx, transport_response
import test_wire

receiver = test_wire.receiver
pytestmark = pytest.mark.skipif(
    not NATIVE, reason="Native telemetry requires SDK >=3.6"
)

TEXT = "permitted-tool-result-text"
TOOL_ID = "screenshot-call"
IMAGES = [
    "https://example.invalid/private-tool-screenshot.png",
    "data:image/png;base64," + "A" * 128,
]


@pytest.mark.parametrize("encoding", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("image", IMAGES, ids=["url", "base64"])
@pytest.mark.parametrize("structured_url", [False, True], ids=["string", "object"])
@pytest.mark.parametrize(
    "policy",
    [
        "visible",
        "hide_input_images",
        "hide_input_text",
        "hide_inputs",
        "hide_input_messages",
        "base64_image_max_length",
    ],
)
def test_tool_images_on_wire(
    receiver, encoding, compressed, image, structured_url, policy
):
    endpoint, requests = receiver
    exporter = (
        test_wire.JSONExporter(endpoint, compressed)
        if encoding == "json"
        else test_wire.OTLPSpanExporter(
            endpoint=endpoint,
            headers={},
            timeout=5,
            compression=(
                test_wire.Compression.Gzip
                if compressed
                else test_wire.Compression.NoCompression
            ),
        )
    )
    config = TraceConfig(
        **(
            {}
            if policy == "visible"
            else {policy: 30 if policy == "base64_image_max_length" else True}
        )
    )
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    content = [
        {"type": "text", "text": TEXT},
        {
            "type": "image_url",
            "image_url": {"url": image, "detail": "low"} if structured_url else image,
        },
        {"type": "text", "text": "trailing-permitted-text"},
    ]
    original_content = copy.deepcopy(content)
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        return transport_response(request)

    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            with Mistral(
                api_key="synthetic-tool-image-key", client=transport
            ) as client:
                result = client.chat.complete(
                    model="mistral-test",
                    messages=[
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": TOOL_ID,
                                    "type": "function",
                                    "function": {
                                        "name": "screenshot",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": TOOL_ID, "content": content},
                    ],
                )
                assert result.choices[0].message.content == "private-output-marker"
        provider.force_flush()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()

    # Redaction is telemetry-only, not a mutation of the caller or SDK request.
    assert content == original_content
    assert observed[0]["messages"][-1]["content"] == original_content
    spans = [
        span
        for _, data in requests
        for resource in data["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert len(spans) == 1
    wire = json.dumps(requests)
    assert "synthetic-tool-image-key" not in wire
    limited = policy == "base64_image_max_length" and image.startswith("data:")
    visible = policy in ("visible", "base64_image_max_length") and not limited
    assert (image in wire) == visible
    assert (TEXT in wire) == (
        policy in ("visible", "base64_image_max_length", "hide_input_images")
    )
    assert "private-output-marker" in wire
    if policy in ("hide_inputs", "hide_input_messages"):
        assert TOOL_ID not in wire
        return
    attrs = {
        attribute["key"]: attribute["value"] for attribute in spans[0]["attributes"]
    }
    messages = json.loads(attrs["gen_ai.input.messages"]["stringValue"])
    part = messages[-1]["parts"][0]
    assert part["type"] == "tool_call_response"
    assert part["id"] == TOOL_ID
    if policy == "hide_input_text":
        assert part["response"] == REDACTED_VALUE
    elif policy == "hide_input_images":
        assert part["response"] == [original_content[0], original_content[2]]
    else:
        expected = copy.deepcopy(original_content)
        if limited:
            if structured_url:
                expected[1]["image_url"]["url"] = REDACTED_VALUE
            else:
                expected[1]["image_url"] = REDACTED_VALUE
        assert part["response"] == expected
