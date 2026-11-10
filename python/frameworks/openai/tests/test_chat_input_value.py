"""Canonical chat input must retain the full conversation."""

import asyncio
from copy import deepcopy
import json

import openai
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()
    try:
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("async_", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stream", [False, True], ids=["ordinary", "streaming"])
@pytest.mark.parametrize(
    "messages, expected_value, expected_mime",
    [
        pytest.param(
            [
                {"role": "system", "content": "SYSTEM_CONTEXT_MARKER"},
                {"role": "user", "content": "USER_QUESTION_MARKER"},
            ],
            "SYSTEM_CONTEXT_MARKER \n USER_QUESTION_MARKER",
            "text/plain",
            id="system-user",
        ),
        pytest.param(
            [
                {"role": "system", "content": "SYSTEM_CONTEXT_MARKER"},
                {"role": "user", "content": "USER_QUESTION_MARKER"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_synthetic",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_synthetic",
                    "content": "TOOL_RESULT_MARKER",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "FOLLOWUP_MARKER café"},
                        {"type": "text", "text": "SECOND_TEXT_PART_MARKER"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://synthetic.invalid/example.png",
                                "detail": "low",
                            },
                        },
                    ],
                },
            ],
            " \n ".join([
                "SYSTEM_CONTEXT_MARKER", "USER_QUESTION_MARKER", "None",
                "TOOL_RESULT_MARKER", "FOLLOWUP_MARKER café",
                "SECOND_TEXT_PART_MARKER", "{{" + f"{SpanAttributes.INPUT_IMAGES}.0" + "}}",
            ]),
            "text/plain",
            id="multiturn-multimodal",
        ),
        pytest.param(
            [{"role": "system", "content": "SYSTEM_CONTEXT_MARKER"}] + [
                message
                for i in range(40)
                for message in (
                    {"role": "user", "content": f"QUESTION_{i}"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps({"query": f"item_{i}"}),
                            },
                        }],
                    },
                    {"role": "tool", "tool_call_id": f"call_{i}", "content": f"RESULT_{i}"},
                    {"role": "assistant", "content": f"ANSWER_{i}"},
                )
            ],
            " \n ".join(["SYSTEM_CONTEXT_MARKER"] + [
                text for i in range(40)
                for text in (f"QUESTION_{i}", "None", f"RESULT_{i}", f"ANSWER_{i}")
            ]),
            "text/plain",
            id="conversation-exceeds-attribute-count",
        ),
        pytest.param(
            [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://synthetic.invalid/a.png"}},
                {"type": "image_url", "image_url": {"url": "https://synthetic.invalid/b.png"}},
            ]}],
            " \n ".join("{{" + f"{SpanAttributes.INPUT_IMAGES}.{i}" + "}}" for i in range(2)),
            "text/plain",
            id="images-only",
        ),
        pytest.param(
            [{"role": "user", "content": [{
                "type": "input_audio", "input_audio": {"data": "AAECAw==", "format": "wav"},
            }]}],
            "[non-text content]",
            "text/plain",
            id="audio-only",
        ),
        pytest.param(
            [{"role": "user", "content": [{
                "type": "file", "file": {"file_data": "AAECAw==", "filename": "sample.bin"},
            }]}],
            "[non-text content]",
            "text/plain",
            id="file-only",
        ),
        pytest.param(
            [{"role": "user", "content": '{"task": "sum"}'}],
            '{"task": "sum"}',
            "application/json",
            id="single-json-message",
        ),
        pytest.param(
            [
                {"role": "user", "content": '{"task": "sum"}'},
                {"role": "user", "content": "now explain it"},
            ],
            '{"task": "sum"} \n now explain it',
            "text/plain",
            id="json-followed-by-text",
        ),
    ],
)
def test_chat_input_value_preserves_full_conversation(
    tracing, async_, stream, messages, expected_value, expected_mime,
):
    original = deepcopy(messages)
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["messages"] == original
        assert body["stream"] is stream
        response = {
            "id": "chatcmpl_synthetic",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ANSWER_MARKER"},
                    "finish_reason": "stop",
                }
            ],
        }
        if stream:
            response["object"] = "chat.completion.chunk"
            choice = response["choices"][0]
            choice["delta"] = choice.pop("message")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=response)

    params = {"model": "gpt-4o-mini", "messages": messages, "stream": stream}

    async def run_async():
        async with openai.AsyncOpenAI(
            api_key="synthetic-test-key",
            base_url="https://synthetic.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            result = await client.chat.completions.create(**params)
            if stream:
                parts = [chunk.choices[0].delta.content or "" async for chunk in result]
                return "".join(parts)
            return result.choices[0].message.content

    if async_:
        answer = asyncio.run(run_async())
    else:
        with openai.OpenAI(
            api_key="synthetic-test-key",
            base_url="https://synthetic.invalid/v1",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            result = client.chat.completions.create(**params)
            if stream:
                answer = "".join(chunk.choices[0].delta.content or "" for chunk in result)
            else:
                answer = result.choices[0].message.content

    assert answer == "ANSWER_MARKER"
    assert len(requests) == 1
    assert messages == original
    (span,) = tracing.get_finished_spans()
    assert span.status.status_code is StatusCode.OK
    attrs = span.attributes
    assert attrs["input.value"] == expected_value
    assert attrs["input.value"].strip()
    assert attrs["input.mime_type"] == expected_mime
    assert json.loads(attrs[SpanAttributes.GEN_AI_INPUT_MESSAGES]) == original
    if len(messages) in (2, 5) and messages[0]["role"] == "system":
        assert attrs["gen_ai.input.messages.1.message.content"] == "USER_QUESTION_MARKER"
    image_urls = [
        part["image_url"]["url"]
        for message in messages if isinstance(message.get("content"), list)
        for part in message["content"] if part["type"] == "image_url"
    ]
    if image_urls:
        assert json.loads(attrs[SpanAttributes.INPUT_IMAGES]) == image_urls


@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_input_messages", "hide_input_text", "hide_input_images"],
)
def test_aggregate_messages_respect_input_privacy(flag):
    from types import SimpleNamespace
    from traceai_openai._request import _WithOpenAI

    request = SimpleNamespace(
        _config=TraceConfig(**{flag: True}),
        _openai=openai,
        _get_span_kind=lambda **kwargs: "LLM",
    )
    attributes = dict(_WithOpenAI._get_attributes_from_request(
        request,
        openai.types.chat.ChatCompletion,
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "PRIVATE_INPUT"}]},
    ))
    assert SpanAttributes.GEN_AI_INPUT_MESSAGES not in attributes
