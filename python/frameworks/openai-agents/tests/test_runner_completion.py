"""Exercise the SDK's actual model tracing for sync-result and streamed runs."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from agents import Agent, OpenAIChatCompletionsModel, Runner, add_trace_processor, trace
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from opentelemetry.trace import StatusCode


@pytest.mark.parametrize("streamed", [False, True])
def test_runner_generation_completion(
    instrument, in_memory_span_exporter, monkeypatch, streamed
):
    async def run():
        # Synthetic credentials and a dead loopback endpoint; create is replaced
        # before the model runs, while the real SDK tracing path stays intact.
        client = AsyncOpenAI(
            api_key="offline-synthetic", base_url="http://127.0.0.1:1/v1"
        )
        response = ChatCompletion(
            id="chatcmpl_offline",
            created=0,
            model="offline-model",
            object="chat.completion",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello back"},
                }
            ],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )

        async def chunks():
            for content, finish_reason in (
                ("hello ", None),
                ("back", None),
                (None, "stop"),
            ):
                yield ChatCompletionChunk(
                    id=response.id,
                    created=0,
                    model=response.model,
                    object="chat.completion.chunk",
                    choices=[
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": content},
                            "finish_reason": finish_reason,
                        }
                    ],
                )
            yield ChatCompletionChunk(
                id=response.id,
                created=0,
                model=response.model,
                object="chat.completion.chunk",
                choices=[],
                usage=response.usage,
            )

        create = AsyncMock(return_value=chunks() if streamed else response)
        monkeypatch.setattr(client.chat.completions, "create", create)
        agent = Agent(
            name="assistant",
            model=OpenAIChatCompletionsModel("offline-model", openai_client=client),
        )
        try:
            with trace("runner"):
                if streamed:
                    result = Runner.run_streamed(agent, "hello")
                    async for _ in result.stream_events():
                        pass
                else:
                    result = await Runner.run(agent, "hello")
                assert result.final_output == "hello back"
            create.assert_awaited_once()
        finally:
            await client.close()

    asyncio.run(run())
    spans = in_memory_span_exporter.get_finished_spans()
    models = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert len(models) == 1
    assert models[0].attributes["gen_ai.usage.input_tokens"] == 3
    assert models[0].attributes["gen_ai.usage.output_tokens"] == 2
    root = next(s for s in spans if s.name == "runner")
    assert json.loads(root.attributes["input.value"])[0]["content"] == "hello"
    output = json.loads(root.attributes["output.value"])[0]
    if streamed:
        assert output["output"][0]["content"][0]["text"] == "hello back"
        assert (
            models[0].attributes[
                "gen_ai.output.messages.0.message.contents.0.message_content.text"
            ]
            == "hello back"
        )
    else:
        assert output["content"] == "hello back"
    assert all(s.end_time is not None for s in spans)
    assert all(s.context.trace_id == root.context.trace_id for s in spans)


def test_runner_error_finishes_model_span(
    instrument, in_memory_span_exporter, monkeypatch
):
    native_errors = []

    def record(span):
        if span.span_data.type == "generation":
            native_errors.append(span.error)

    add_trace_processor(Mock(on_span_end=record))

    async def run():
        client = AsyncOpenAI(
            api_key="offline-synthetic", base_url="http://127.0.0.1:1/v1"
        )
        monkeypatch.setattr(
            client.chat.completions,
            "create",
            AsyncMock(side_effect=RuntimeError("offline failure")),
        )
        agent = Agent(
            name="assistant",
            model=OpenAIChatCompletionsModel("offline-model", openai_client=client),
        )
        try:
            with pytest.raises(RuntimeError, match="offline failure"):
                await Runner.run(agent, "hello")
        finally:
            await client.close()

    asyncio.run(run())
    spans = in_memory_span_exporter.get_finished_spans()
    models = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert len(models) == 1
    assert len(native_errors) == 1
    # 0.2.6 does not attach this exception to its GenerationSpanData. A processor
    # cannot invent an error that the SDK never reports; preserve the native status.
    expected = StatusCode.ERROR if native_errors[0] else StatusCode.OK
    assert models[0].status.status_code == expected
    assert all(s.end_time is not None for s in spans)
