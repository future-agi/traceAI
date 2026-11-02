"""Privacy and multi-result contracts using real SDKs and synthetic transports."""

import copy
import json
from contextlib import contextmanager

import httpx
import litellm
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_litellm import LiteLLMInstrumentor


@pytest.fixture(autouse=True)
def offline_tokenizers(monkeypatch):
    # LiteLLM's background cost accounting must not download tokenizers.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")


@contextmanager
def capture(config=None):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    inst = LiteLLMInstrumentor()
    inst.instrument(tracer_provider=provider, config=config or TraceConfig())
    try:
        yield exporter
    finally:
        inst.uninstrument()
        provider.shutdown()


def attrs(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.OK
    return dict(spans[0].attributes)


def image_messages(marker, with_tool):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this synthetic image"},
                {"type": "image_url", "image_url": {"url": marker}},
            ],
        }
    ]
    if with_tool:
        messages += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "synthetic_call",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "synthetic_call",
                "content": "synthetic-result",
            },
        ]
    return messages


@pytest.mark.parametrize("with_tool", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("from_environment", [False, True])
@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.asyncio
async def test_hide_input_images_with_tool_history(
    with_tool, asynchronous, from_environment, hide, monkeypatch
):
    marker = "data:image/png;base64,c3ludGhldGljLXByaXZhdGUtaW1hZ2U="
    messages = image_messages(marker, with_tool)
    if from_environment:
        monkeypatch.setenv("FI_HIDE_INPUT_IMAGES", str(hide).lower())
        config = TraceConfig()
    else:
        # Explicit configuration must override the environment in both directions.
        monkeypatch.setenv("FI_HIDE_INPUT_IMAGES", str(not hide).lower())
        config = TraceConfig(hide_input_images=hide)
    original = copy.deepcopy(messages)
    with capture(config) as exporter:
        kwargs = dict(
            model="gpt-4o-mini",
            messages=messages,
            mock_response="hello",
            api_key="synthetic-key",
        )
        result = (
            await litellm.acompletion(**kwargs)
            if asynchronous
            else litellm.completion(**kwargs)
        )
    assert result.choices[0].message.content == "hello"
    assert messages == original
    actual = attrs(exporter)
    assert marker not in actual["input.value"]
    assert (marker in json.dumps(actual)) is not hide
    if with_tool:
        conversation = json.loads(actual["input.value"])
        assert conversation[1:] == messages[1:]
        assert "{{gen_ai.input.images.0}}" in json.dumps(conversation[0])
    if hide:
        assert "gen_ai.input.images" not in actual
    else:
        assert json.loads(actual["gen_ai.input.images"]) == [marker]
        assert (
            actual[
                "gen_ai.input.messages.0.message.contents.1.message_content.image.image.url"
            ]
            == marker
        )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("from_environment", [False, True])
@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.parametrize(
    "inputs,expected_texts",
    [
        ("synthetic-private-single", ["synthetic-private-single"]),
        (
            ["synthetic-private-first", "synthetic-private-second"],
            ["synthetic-private-first", "synthetic-private-second"],
        ),
        ([101, 102], ["[101, 102]"]),
        ([[101, 102], [103, 104]], ["[101, 102]", "[103, 104]"]),
    ],
)
@pytest.mark.asyncio
async def test_embedding_inputs_match_vectors_and_respect_privacy(
    asynchronous, from_environment, hide, inputs, expected_texts, monkeypatch
):
    # Provider indices, not response list positions, associate vectors with inputs.
    data = [
        {"object": "embedding", "index": i, "embedding": [i + 0.1, i + 0.2]}
        for i in reversed(range(len(expected_texts)))
    ]
    payload = {
        "object": "list",
        "model": "text-embedding-ada-002",
        "data": data,
        "usage": {"prompt_tokens": 2, "total_tokens": 2},
    }
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(respond)
    if from_environment:
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hide).lower())
        config = TraceConfig()
    else:
        monkeypatch.setenv("FI_HIDE_INPUTS", str(not hide).lower())
        config = TraceConfig(hide_inputs=hide)
    original = copy.deepcopy(inputs)
    with capture(config) as exporter:
        if asynchronous:
            async with openai.AsyncOpenAI(
                api_key="synthetic-key",
                http_client=httpx.AsyncClient(transport=transport),
            ) as client:
                result = await litellm.aembedding(
                    "text-embedding-ada-002", inputs, client=client
                )
        else:
            with openai.OpenAI(
                api_key="synthetic-key", http_client=httpx.Client(transport=transport)
            ) as client:
                result = litellm.embedding(
                    "text-embedding-ada-002", inputs, client=client
                )
    assert inputs == original
    assert requests[0]["input"] == inputs
    assert result.data == data
    actual = attrs(exporter)
    assert actual["input.value"] == ("__REDACTED__" if hide else str(inputs))
    for index, text in enumerate(expected_texts):
        assert actual[f"embedding.embeddings.{index}.embedding.text"] == (
            "__REDACTED__" if hide else text
        )
        assert json.loads(actual[f"embedding.embeddings.{index}.embedding.vector"]) == [
            index + 0.1,
            index + 0.2,
        ]
        if hide:
            assert text not in json.dumps(actual)
    assert f"embedding.embeddings.{len(expected_texts)}.embedding.text" not in actual
    assert actual["gen_ai.usage.total_tokens"] == 2


@pytest.mark.parametrize("with_tool", [False, True])
@pytest.mark.parametrize("limit", [None, 32])
@pytest.mark.parametrize("image_kind", ["oversized", "at-limit", "remote"])
def test_base64_image_size_limit(with_tool, limit, image_kind):
    max_length = limit if limit is not None else 32000
    prefix = "data:image/png;base64,"
    marker = prefix + "A" * (max_length - len(prefix) + (image_kind == "oversized"))
    if image_kind == "remote":
        marker = "https://example.invalid/" + "x" * 100 + ".png"
    messages = image_messages(marker, with_tool)
    original = copy.deepcopy(messages)
    with capture(TraceConfig(base64_image_max_length=limit)) as exporter:
        result = litellm.completion(
            model="gpt-4o-mini",
            messages=messages,
            mock_response="hello",
            api_key="synthetic-key",
        )
    assert result.choices[0].message.content == "hello"
    assert messages == original
    actual = attrs(exporter)
    expected = "__REDACTED__" if image_kind == "oversized" else marker
    assert (
        actual[
            "gen_ai.input.messages.0.message.contents.1.message_content.image.image.url"
        ]
        == expected
    )
    assert json.loads(actual["gen_ai.input.images"]) == [expected]
    assert marker not in actual["input.value"]
    if image_kind == "oversized":
        assert marker not in json.dumps(actual)
    if with_tool:
        assert json.loads(actual["input.value"])[1:] == messages[1:]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.asyncio
async def test_multiple_completion_choices_are_preserved(streaming, asynchronous, hide):
    messages = [{"role": "user", "content": "synthetic-question"}]
    texts = ["synthetic-choice-one", "synthetic-choice-two"]
    payload = {
        "id": "synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": i,
                "message": {"role": "assistant", "content": texts[i]},
                "finish_reason": "stop",
            }
            for i in (0, 1)
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6},
    }
    if streaming:
        chunks = []
        # Split and interleave choices; every chunk need not contain choice zero.
        for part in (0, 1):
            for index in (1, 0):
                delta = {
                    "content": texts[index][:10] if part == 0 else texts[index][10:]
                }
                if part == 0:
                    delta["role"] = "assistant"
                chunks.append(
                    {
                        "id": "synthetic",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "gpt-4o-mini",
                        "choices": [
                            {
                                "index": index,
                                "delta": delta,
                                "finish_reason": "stop" if part else None,
                            }
                        ],
                    }
                )
        chunks.append(
            {
                "id": "synthetic",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [],
                "usage": payload["usage"],
            }
        )
        raw = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        ).encode()
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=raw
            )
        )
    else:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        )
    kwargs = dict(model="gpt-4o-mini", messages=messages, n=2, stream=streaming)
    if streaming:
        kwargs["stream_options"] = {"include_usage": True}
    with capture(TraceConfig(hide_outputs=hide)) as exporter:
        if asynchronous:
            async with openai.AsyncOpenAI(
                api_key="synthetic-key",
                http_client=httpx.AsyncClient(transport=transport),
            ) as client:
                result = await litellm.acompletion(**kwargs, client=client)
                if streaming:
                    received = [chunk async for chunk in result]
        else:
            with openai.OpenAI(
                api_key="synthetic-key", http_client=httpx.Client(transport=transport)
            ) as client:
                result = litellm.completion(**kwargs, client=client)
                if streaming:
                    received = list(result)
    roles = {}
    if streaming:
        content_by_choice = {}
        for chunk in received:
            for choice in chunk.choices:
                content_by_choice.setdefault(choice.index, "")
                content_by_choice[choice.index] += choice.delta.content or ""
                if choice.delta.role:
                    roles[choice.index] = choice.delta.role
        assert content_by_choice == dict(enumerate(texts))
    else:
        assert {
            choice.index: choice.message.content for choice in result.choices
        } == dict(enumerate(texts))
        roles = {choice.index: choice.message.role for choice in result.choices}
    actual = attrs(exporter)
    if hide:
        assert actual["output.value"] == "__REDACTED__"
        for text in texts:
            assert text not in json.dumps(actual)
        assert not any(key.startswith("gen_ai.output.messages.") for key in actual)
    else:
        for index, text in enumerate(texts):
            assert actual[f"gen_ai.output.messages.{index}.message.content"] == text
            if index in roles:
                assert (
                    actual[f"gen_ai.output.messages.{index}.message.role"]
                    == roles[index]
                )
        output = json.loads(actual["output.value"])
        choices = (
            [choice for chunk in output for choice in chunk["choices"]]
            if streaming
            else output["choices"]
        )
        assert {choice["index"] for choice in choices} == {0, 1}
    assert actual["gen_ai.usage.total_tokens"] == 6


@pytest.mark.parametrize("streaming", [False, True])
def test_error_privacy_is_preserved(streaming, monkeypatch):
    marker = "synthetic-private-error-detail"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400, json={"error": {"message": marker, "type": "invalid_request_error"}}
        )
    )
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    with openai.OpenAI(
        api_key="synthetic-key",
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    ) as client:
        with capture() as exporter:
            with pytest.raises(Exception):
                litellm.completion(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=streaming,
                    client=client,
                    num_retries=0,
                )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert marker not in spans[0].to_json()


def test_image_sanitization_preserves_other_tool_conversation_content():
    marker = "data:image/png;base64,c3ludGhldGlj"
    messages = image_messages(marker, True)
    audio = {
        "type": "input_audio",
        "input_audio": {"data": "synthetic-audio", "format": "wav"},
    }
    # No text in the multimodal message: it must not fall back to its raw images.
    messages[0]["content"] = [messages[0]["content"][1], audio]
    original = copy.deepcopy(messages)
    with capture(TraceConfig(hide_input_images=True)) as exporter:
        litellm.completion(
            model="gpt-4o-mini",
            messages=messages,
            mock_response="hello",
            api_key="synthetic-key",
        )
    actual = attrs(exporter)
    conversation = json.loads(actual["input.value"])
    assert audio in conversation[0]["content"]
    assert conversation[1:] == original[1:]
    assert marker not in json.dumps(actual)
    assert messages == original
