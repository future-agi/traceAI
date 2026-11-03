"""Independent review checks: synthetic requests through installed SDKs only."""

import asyncio
import json

import pytest
from opentelemetry.trace import StatusCode

from test_review_regressions import tracing, Mistral, httpx, NATIVE, CHAT, EventStream


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["chat", "embedding"])
def test_nonstream_validation_failure_is_error(asynchronous, operation):
    def respond(request):
        return httpx.Response(
            200, json={"error": {"message": "synthetic-private-error"}}
        )

    def invoke(client):
        if operation == "embedding":
            method = (
                client.embeddings.create_async
                if asynchronous
                else client.embeddings.create
            )
            return method(model="mistral-embed", inputs=["hello"])
        method = client.chat.complete_async if asynchronous else client.chat.complete
        return method(
            model="mistral-test", messages=[{"role": "user", "content": "hello"}]
        )

    with tracing(hide_error_details=True) as (exporter, starts):
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    with pytest.raises(Exception) as error:
                        await invoke(
                            Mistral(api_key="synthetic", async_client=transport)
                        )
                    assert "Validation" in type(error.value).__name__

            asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                with pytest.raises(Exception) as error:
                    invoke(Mistral(api_key="synthetic", client=transport))
                assert "Validation" in type(error.value).__name__
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
        assert "synthetic-private-error" not in spans[0].to_json()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "flag,marker",
    [
        ("hide_input_text", "synthetic-private-prompt"),
        ("hide_input_messages", "synthetic-private-prompt"),
        ("hide_output_text", "synthetic-private-answer"),
        ("hide_output_messages", "synthetic-private-answer"),
        ("hide_inputs", "synthetic-private-prompt"),
        ("hide_outputs", "synthetic-private-answer"),
    ],
)
def test_selective_chat_privacy(asynchronous, flag, marker):
    response = json.loads(json.dumps(CHAT))
    response["choices"][0]["message"]["content"] = "synthetic-private-answer"

    def respond(request):
        assert (
            json.loads(request.content)["messages"][0]["content"]
            == "synthetic-private-prompt"
        )
        return httpx.Response(200, json=response)

    kwargs = dict(
        model="mistral-test",
        messages=[{"role": "user", "content": "synthetic-private-prompt"}],
    )
    with tracing(**{flag: True}) as (exporter, starts):
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    result = await Mistral(
                        api_key="synthetic", async_client=transport
                    ).chat.complete_async(**kwargs)
                    assert (
                        result.choices[0].message.content == "synthetic-private-answer"
                    )

            asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                result = Mistral(api_key="synthetic", client=transport).chat.complete(
                    **kwargs
                )
                assert result.choices[0].message.content == "synthetic-private-answer"
        (span,) = exporter.get_finished_spans()
        assert marker not in span.to_json()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("configured", ["argument", "environment", "visible"])
def test_hide_invocation_parameters(asynchronous, configured, monkeypatch):
    monkeypatch.setenv(
        "FI_HIDE_LLM_INVOCATION_PARAMETERS",
        "true" if configured == "environment" else "false",
    )
    flags = {"hide_llm_invocation_parameters": True} if configured == "argument" else {}
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=CHAT)

    kwargs = dict(
        model="mistral-test",
        messages=[{"role": "user", "content": "hello"}],
        stop=["synthetic-private-stop"],
        temperature=0.731,
    )
    with tracing(**flags) as (exporter, starts):
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    await Mistral(
                        api_key="synthetic", async_client=transport
                    ).chat.complete_async(**kwargs)

            asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                Mistral(api_key="synthetic", client=transport).chat.complete(**kwargs)
        (span,) = exporter.get_finished_spans()
        assert observed[0]["stop"] == ["synthetic-private-stop"]
        assert observed[0]["temperature"] == 0.731
        assert span.attributes["gen_ai.usage.input_tokens"] == 2
        if NATIVE:
            assert span.attributes["gen_ai.request.model"] == "mistral-test"
        if configured == "visible":
            if NATIVE:
                assert span.attributes["gen_ai.request.temperature"] == 0.731
                assert span.attributes["gen_ai.request.stop_sequences"] == (
                    "synthetic-private-stop",
                )
            else:
                params = json.loads(span.attributes["gen_ai.request.parameters"])
                assert params["temperature"] == 0.731
                assert params["stop"] == ["synthetic-private-stop"]
        else:
            assert "gen_ai.request.temperature" not in span.attributes
            assert "synthetic-private-stop" not in span.to_json()


@pytest.mark.skipif(not NATIVE, reason="Native conversations")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_unicode_conversation_content_preserved(asynchronous, separator, newline):
    marker = "first" + separator + "second"
    events = [
        {
            "type": "message.output.delta",
            "id": "msg-1",
            "role": "assistant",
            "content": marker,
        },
        {
            "type": "conversation.response.done",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
    ]

    class RawEvents(EventStream):
        def __iter__(self):
            for event in self.events:
                yield (
                    "event: "
                    + event["type"]
                    + newline
                    + "data: "
                    + json.dumps(event, ensure_ascii=False)
                    + newline * 2
                ).encode()

    def respond(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=RawEvents(events)
        )

    with tracing() as (exporter, starts):
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    async with await Mistral(
                        api_key="synthetic", async_client=transport
                    ).beta.conversations.start_stream_async(
                        model="mistral-test", inputs="hello"
                    ) as stream:
                        result = [event async for event in stream]
                    assert result[0].data.content == marker

            asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                with Mistral(
                    api_key="synthetic", client=transport
                ).beta.conversations.start_stream(
                    model="mistral-test", inputs="hello"
                ) as stream:
                    result = list(stream)
                assert result[0].data.content == marker
        (span,) = exporter.get_finished_spans()
        assert json.loads(span.attributes["gen_ai.output.messages"])[0]["parts"] == [
            {"type": "text", "content": marker}
        ]
