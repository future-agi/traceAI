"""Cancellation and restoration of the SDK's native request-span lifecycle."""

import asyncio
import gc
import weakref

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_mistralai import MistralAIInstrumentor
from test_sdk import NATIVE, Mistral, httpx, params, transport_response

pytestmark = pytest.mark.skipif(
    not NATIVE, reason="Native telemetry requires SDK >=3.6"
)


def request(client, operation):
    if operation == "embedding":
        return client.embeddings.create_async(model="mistral-embed", inputs=["hello"])
    if operation.startswith("conversation"):
        method = (
            client.beta.conversations.start_stream_async
            if operation.endswith("stream")
            else client.beta.conversations.start_async
        )
        return method(model="mistral-test", inputs="hello")
    method = (
        client.chat.stream_async
        if operation == "chat-stream"
        else client.chat.complete_async
    )
    return method(**params("chat"))


@pytest.mark.parametrize(
    "operation",
    ["chat", "chat-stream", "embedding", "conversation", "conversation-stream"],
)
@pytest.mark.parametrize("timeout", [False, True], ids=["cancel", "timeout"])
def test_cancelled_native_send_ends_only_its_span(operation, timeout):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    started = []

    class RecordingProcessor(SimpleSpanProcessor):
        def on_start(self, span, parent_context=None):
            started.append(span)

    provider.add_span_processor(RecordingProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(hide_error_details=True)
    )

    async def run():
        entered = asyncio.Event()
        requests = []

        async def respond(http_request):
            requests.append(http_request)
            if len(requests) == 1:
                entered.set()
                await asyncio.Event().wait()
            return transport_response(http_request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            client = Mistral(api_key="synthetic", async_client=transport)
            task = asyncio.create_task(request(client, operation))
            await asyncio.wait_for(entered.wait(), 5)
            # A successful request sharing the client must not be ended by cancellation.
            result = await client.chat.complete_async(**params("chat"))
            assert result.choices[0].message.content == "private-output-marker"
            if timeout:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, 0.01)
            else:
                task.cancel("private-cancellation-marker")
                # Python 3.10 drops Task.cancel(msg) at the task-await boundary,
                # including without instrumentation. Direct-await tests below
                # still require the original cancellation message to survive.
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert trace.get_current_span() is trace.INVALID_SPAN
            assert len(started) == 2
            assert all(not span.is_recording() for span in started)
            spans = exporter.get_finished_spans()
            assert len(spans) == 2
            assert spans[0].status.status_code == trace.StatusCode.OK
            assert spans[1].status.status_code == trace.StatusCode.ERROR
            assert spans[1].context.span_id == started[0].context.span_id
            assert "private-cancellation-marker" not in spans[1].to_json()
            assert requests[0].extensions["_tracing_span"] is None
            assert requests[0].extensions["_tracing_span_finished"] is True
        assert len(exporter.get_finished_spans()) == 2

    try:
        asyncio.run(run())
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_cancelled_send_preserves_cancellation_if_telemetry_fails(monkeypatch, caplog):
    from mistralai.client._hooks.tracing import TracingHook

    caplog.set_level("DEBUG", logger="traceai_mistralai._native")

    def fail(*args, **kwargs):
        raise RuntimeError("private-telemetry-marker")

    monkeypatch.setattr(TracingHook, "_finish_request_span_with_error", fail)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider)

    async def run():
        async def respond(http_request):
            raise asyncio.CancelledError("original-cancellation")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            client = Mistral(api_key="synthetic", async_client=transport)
            with pytest.raises(asyncio.CancelledError, match="original-cancellation"):
                await client.chat.complete_async(**params("chat"))

    try:
        asyncio.run(run())
        assert "Could not finish cancelled Mistral request" in caplog.text
        assert "private-telemetry-marker" not in caplog.text
        assert "original-cancellation" not in caplog.text
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("reconfigured", [False, True])
def test_native_async_send_restored_without_retaining_clients(reconfigured):
    from mistralai.extra.observability import configure_telemetry

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    other = TracerProvider()
    instrumentor = MistralAIInstrumentor()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_response)
        ) as transport:
            client = Mistral(api_key="synthetic", async_client=transport)
            configure_telemetry(client, provider=other, redaction=False)
            await client.chat.complete_async(**params("chat"))
            original = transport.send  # Includes the SDK's own context/error wrapper.
            for _ in range(2):
                instrumentor.instrument(tracer_provider=provider)
                await client.chat.complete_async(**params("chat"))
                assert transport.send is not original
                if reconfigured:

                    async def replacement(*args, **kwargs):
                        return await original(*args, **kwargs)

                    transport.send = replacement
                instrumentor.uninstrument()
                if reconfigured:
                    assert transport.send is replacement
                    transport.send = original
                else:
                    assert transport.send is original
            assert len(exporter.get_finished_spans()) == 2
            # Leave instrumentation on when dropping the client to check weak tracking.
            instrumentor.instrument(tracer_provider=provider)
            await client.chat.complete_async(**params("chat"))
            return weakref.ref(transport)

    try:
        client_ref = asyncio.run(run())
        gc.collect()  # Run the SDK finalizer, which itself holds the HTTP client.
        gc.collect()  # Collect cycles released by that finalizer.
        assert client_ref() is None
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        other.shutdown()
