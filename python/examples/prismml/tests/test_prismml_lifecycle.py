"""Release gates for maintained-instrumentor lifecycle behavior.

These contracts must pass with the recipe pins, not only an unreleased source tree.
Shared repairs live in traceAI #520/#536; keep this route blocked until released.
"""

import asyncio
import json
import os

import app
from _fake_openai import chat_response, stream_body
from harness import Receiver
import httpx
import openai
import pytest
from traceai_openai import OpenAIInstrumentor


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("FI_", "OTEL_", "OPENAI_", "PRISMML_")) or name.lower() in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FI_API_KEY", "synthetic-review-fi-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-review-fi-secret")
    monkeypatch.setenv("NO_PROXY", "*")
    yield
    if OpenAIInstrumentor().is_instrumented_by_opentelemetry:
        OpenAIInstrumentor().uninstrument()


@pytest.fixture
def traced(monkeypatch):
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        provider = app.setup_tracing("prismml-independent-review")
        try:
            yield receiver, provider
        finally:
            OpenAIInstrumentor().uninstrument()
            provider.shutdown()


def client_factory(handler):
    return lambda **kwargs: openai.OpenAI(
        **kwargs,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def attrs(span):
    return {
        item["key"]: next(iter(item["value"].values()))
        for item in span.get("attributes", [])
    }


def first_chunk():
    return stream_body().split(b"\n\n", 1)[0] + b"\n\n"


@pytest.mark.parametrize("stream", [False, True])
def test_exact_app_interrupt_exports_error(monkeypatch, traced, stream):
    receiver, provider = traced
    monkeypatch.setattr(app, "setup_tracing", lambda: provider)

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield first_chunk()
            raise KeyboardInterrupt("synthetic interrupt")

    def handler(request):
        if not stream:
            raise KeyboardInterrupt("synthetic interrupt")
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Interrupted()
        )

    monkeypatch.setattr(app, "OpenAI", client_factory(handler))
    with pytest.raises(KeyboardInterrupt):
        app.main(["--stream"] if stream else [])
    provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1, (
        f"Interrupted app exported {len(spans)} spans after its finally flush"
    )
    assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR"


@pytest.mark.parametrize("terminator", [b"", b"data: [DONE]\n\n"])
def test_exact_app_incomplete_stream_is_not_success(monkeypatch, traced, terminator):
    receiver, provider = traced
    monkeypatch.setattr(app, "setup_tracing", lambda: provider)

    def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=first_chunk() + terminator,
        )

    monkeypatch.setattr(app, "OpenAI", client_factory(handler))
    outcome = app.main(["--stream"])
    assert outcome == 1
    provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    status = spans[0].get("status", {}).get("code")
    assert status == "STATUS_CODE_ERROR", (
        f"Incomplete reply: app returned {outcome}, span status={status}, output={attrs(spans[0]).get('output.value')!r}"
    )


def test_explicit_stream_close_finishes_span(traced):
    receiver, provider = traced
    with client_factory(
        lambda req: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=stream_body()
        )
    )(base_url="http://127.0.0.1:9/v1", api_key="synthetic-model-key") as client:
        stream = client.chat.completions.create(
            model="bonsai",
            messages=[{"role": "user", "content": "safe prompt"}],
            stream=True,
        )
        next(stream)
        stream.close()
    provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1, f"Explicitly closed stream exported {len(spans)} spans"
    assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR"


def test_async_request_cancellation_finishes_span(traced):
    receiver, provider = traced

    async def run():
        async def handler(request):
            raise asyncio.CancelledError("synthetic cancellation")

        async with openai.AsyncOpenAI(
            base_url="http://127.0.0.1:9/v1",
            api_key="synthetic-model-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            with pytest.raises(asyncio.CancelledError):
                await client.chat.completions.create(
                    model="bonsai",
                    messages=[{"role": "user", "content": "safe prompt"}],
                )

    asyncio.run(run())
    provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1, f"Cancelled async request exported {len(spans)} spans"
    assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR"


def test_parent_context_and_double_instrumentation(traced):
    receiver, provider = traced
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    tracer = provider.get_tracer("synthetic-parent")
    with tracer.start_as_current_span("parent"):
        with client_factory(lambda req: httpx.Response(200, json=chat_response()))(
            base_url="http://127.0.0.1:9/v1", api_key="synthetic-model-key"
        ) as client:
            client.chat.completions.create(
                model="bonsai", messages=[{"role": "user", "content": "safe prompt"}]
            )
    provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 2
    parent = next(s for s in spans if s["name"] == "parent")
    child = next(s for s in spans if s["name"] == "ChatCompletion")
    assert child["parentSpanId"] == parent["spanId"]
    assert child["traceId"] == parent["traceId"]
    assert attrs(child)["gen_ai.span.kind"] == "LLM"


@pytest.mark.parametrize("stream", [False, True])
def test_async_success_has_one_span(traced, stream):
    receiver, provider = traced

    async def run():
        def handler(request):
            return (
                httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=stream_body(),
                )
                if stream
                else httpx.Response(200, json=chat_response())
            )

        async with openai.AsyncOpenAI(
            base_url="http://127.0.0.1:9/v1",
            api_key="synthetic-model-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            response = await client.chat.completions.create(
                model="bonsai",
                messages=[{"role": "user", "content": "safe prompt"}],
                stream=stream,
            )
            if stream:
                async for _ in response:
                    pass

    asyncio.run(run())
    provider.force_flush()
    assert len(receiver.spans()) == 1
    assert receiver.spans()[0]["status"]["code"] == "STATUS_CODE_OK"


def test_hidden_errors_do_not_export_content(monkeypatch):
    for key in (
        "FI_HIDE_INPUTS",
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_LLM_INVOCATION_PARAMETERS",
    ):
        monkeypatch.setenv(key, "true")
    marker = "synthetic-private-prompt-and-answer"
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        provider = app.setup_tracing("prismml-error-privacy")
        try:
            with client_factory(
                lambda req: httpx.Response(400, json={"error": {"message": marker}})
            )(
                base_url="http://127.0.0.1:9/v1", api_key="synthetic-model-key"
            ) as client:
                with pytest.raises(openai.BadRequestError):
                    client.chat.completions.create(
                        model="bonsai", messages=[{"role": "user", "content": marker}]
                    )
            provider.force_flush()
            spans = receiver.spans()
            assert len(spans) == 1
            assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR"
            assert marker not in json.dumps(spans)
        finally:
            OpenAIInstrumentor().uninstrument()
            provider.shutdown()


@pytest.mark.parametrize(
    "body",
    [b"", first_chunk(), first_chunk() + b"data: [DONE]\n\n", b"data: [DONE]\n\n"],
)
def test_cli_rejects_missing_terminal_choice(monkeypatch, traced, capsys, body):
    _, provider = traced
    monkeypatch.setattr(app, "setup_tracing", lambda: provider)
    response = httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, content=body
    )
    monkeypatch.setattr(app, "OpenAI", client_factory(lambda request: response))
    assert app.main(["--stream"]) == 1
    assert "before a finish reason" in capsys.readouterr().err
    assert response.is_closed


@pytest.mark.parametrize("reason", ["stop", "length", "tool_calls", "content_filter"])
@pytest.mark.parametrize("usage_tail", [False, True])
def test_cli_accepts_terminal_choice_and_usage_tail(
    monkeypatch, traced, reason, usage_tail
):
    _, provider = traced
    monkeypatch.setattr(app, "setup_tracing", lambda: provider)
    terminal = {
        "id": "synthetic-terminal",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "bonsai",
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
    }
    body = first_chunk() + ("data: " + json.dumps(terminal) + "\n\n").encode()
    if usage_tail:
        usage = dict(
            terminal,
            choices=[],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )
        body += ("data: " + json.dumps(usage) + "\n\n").encode()
    body += b"data: [DONE]\n\n"
    response = httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, content=body
    )
    monkeypatch.setattr(app, "OpenAI", client_factory(lambda request: response))
    assert app.main(["--stream"]) == 0
    assert response.is_closed


def test_async_stream_cancellation_finishes_span(traced):
    receiver, provider = traced
    interruption = asyncio.CancelledError("synthetic stream cancellation")

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first_chunk()
            raise interruption

    async def run():
        async def handler(request):
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=Interrupted()
            )

        async with openai.AsyncOpenAI(
            base_url="http://127.0.0.1:9/v1",
            api_key="synthetic-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            stream = await client.chat.completions.create(
                model="bonsai",
                messages=[{"role": "user", "content": "safe"}],
                stream=True,
            )
            try:
                with pytest.raises(asyncio.CancelledError) as caught:
                    async for _ in stream:
                        pass
                assert caught.value is interruption
            finally:
                await stream.close()

    asyncio.run(run())
    provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR"


def test_async_explicit_close_finishes_span_once(traced):
    receiver, provider = traced

    async def run():
        async def handler(request):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=stream_body(),
            )

        async with openai.AsyncOpenAI(
            base_url="http://127.0.0.1:9/v1",
            api_key="synthetic-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            stream = await client.chat.completions.create(
                model="bonsai",
                messages=[{"role": "user", "content": "safe"}],
                stream=True,
            )
            await stream.__anext__()
            await stream.close()
            await stream.close()

    asyncio.run(run())
    provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR"
