"""Text API delegation and partially consumed streams retain shared lifecycle guarantees."""

import json

import litellm
import pytest
from opentelemetry.trace import StatusCode

from test_stream_extensions import capture, consume, invoke  # noqa: F401


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_text_stream_failure(capture, asynchronous):
    failure = RuntimeError("synthetic-midstream-failure")
    chunks = [
        {
            "object": "text_completion",
            "choices": [{"index": 0, "text": "partial", "finish_reason": None}],
        },
        failure,
    ]
    with capture(chunks=chunks, endpoint="text_completion") as (exporter, _):
        stream = await invoke(
            asynchronous, endpoint="text_completion", prompt="hello", stream=True
        )
        with pytest.raises(RuntimeError) as caught:
            await consume(stream, asynchronous)
        assert caught.value is failure
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert spans[0].attributes["output.value"] == "partial"
    assert "gen_ai.response.finish_reasons" not in spans[0].attributes


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_sparse_interleaved_finish_reasons(capture, asynchronous, streaming):
    chunks = [
        {
            "choices": [
                {"index": 3, "delta": {"content": "third"}, "finish_reason": None}
            ]
        },
        {
            "choices": [
                {"index": 1, "delta": {"content": "first"}, "finish_reason": "length"}
            ]
        },
        {"choices": [{"index": 3, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [{"index": 1, "delta": {}, "finish_reason": None}]},
        {"choices": [], "usage": {"total_tokens": 7}},
    ]
    result = {
        "choices": [
            {"index": 3, "text": "third", "finish_reason": "stop"},
            {"index": 1, "text": "first", "finish_reason": "length"},
        ]
    }
    with capture(chunks=chunks if streaming else None, result=result) as (exporter, _):
        response = await invoke(asynchronous, stream=streaming)
        if streaming:
            await consume(response, asynchronous)
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["gen_ai.response.finish_reasons"] == ("length", "stop")
    assert attrs["gen_ai.output.messages.1.message.finish_reason"] == "length"
    assert attrs["gen_ai.output.messages.3.message.finish_reason"] == "stop"
    assert "gen_ai.output.messages.0.message.finish_reason" not in attrs


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_function_schema_is_not_treated_as_connection_credentials(
    capture, asynchronous
):
    function = {
        "name": "authorize",
        "parameters": {
            "type": "object",
            "properties": {
                "authorization": {"type": "string"},
                "headers": {"type": "object"},
            },
        },
    }
    tools = [{"type": "function", "function": function}]
    with capture(result={"choices": []}) as (exporter, _):
        await invoke(asynchronous, tools=tools)
    params = json.loads(
        exporter.get_finished_spans()[0].attributes["gen_ai.request.parameters"]
    )
    assert params["tools"] == tools


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_unknown_builtin_connection_fields_are_not_exported(
    capture, asynchronous
):
    tools = [
        {
            "type": "future_builtin",
            "headers": {"x-provider-token": "synthetic-secret"},
            "connection": {"authorization": "synthetic-secret"},
        }
    ]
    with capture(result={"choices": []}) as (exporter, _):
        await invoke(asynchronous, tools=tools)
    span = exporter.get_finished_spans()[0]
    assert "synthetic-secret" not in span.to_json()
    params = json.loads(span.attributes["gen_ai.request.parameters"])
    assert params["tools"] == [{"type": "future_builtin"}]
    assert tools[0]["headers"]["x-provider-token"] == "synthetic-secret"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_text_delegation_emits_one_span(capture, monkeypatch, asynchronous):
    result = {
        "object": "text_completion",
        "choices": [{"index": 0, "text": "hello", "finish_reason": "stop"}],
    }

    def delegated(prompt, model=None, **kwargs):
        return litellm.completion(
            model=model, messages=[{"role": "user", "content": prompt}]
        )

    async def adelegated(*args, **kwargs):
        return litellm.text_completion(*args, **kwargs)

    monkeypatch.setattr(litellm, "text_completion", delegated)
    monkeypatch.setattr(litellm, "atext_completion", adelegated)
    original = litellm.text_completion
    with capture(result=result) as (exporter, _):
        if asynchronous:
            response = await litellm.atext_completion(
                "positional prompt", model="openai/gpt-4o-mini"
            )
        else:
            response = litellm.text_completion(
                "positional prompt", model="openai/gpt-4o-mini"
            )
        assert response is result
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == ("atext_completion" if asynchronous else "text_completion")
    input_messages = json.loads(spans[0].attributes["input.value"])["messages"]
    assert input_messages[0]["content"] == "positional prompt"
    assert litellm.text_completion is original
