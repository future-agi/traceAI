"""Native HTTP request interruption must finish the span before propagating."""

import os

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as http
else:
    import httpx as http


@pytest.mark.parametrize("base_url", [
    "https://model-synthetic.api.baseten.co/sync/v1",
    "https://model-synthetic-us-west.api.baseten.co/sync/v1",
])
@pytest.mark.parametrize("hide_errors", [False, True])
@pytest.mark.parametrize("interrupt", [False, True], ids=["success-control", "interrupted"])
def test_sync_request_interrupt_finishes_span(base_url, hide_errors, interrupt, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_", "OPENAI_")):
            monkeypatch.delenv(name)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(hide_error_details=hide_errors),
    )
    error = KeyboardInterrupt("synthetic request interruption")

    def handle(_):
        if interrupt:
            raise error
        return http.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "served-model", "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        })

    try:
        with openai.OpenAI(
            api_key="synthetic", base_url=base_url, max_retries=0,
            http_client=http.Client(transport=http.MockTransport(handle), trust_env=False),
        ) as client:
            if interrupt:
                with pytest.raises(KeyboardInterrupt) as caught:
                    client.chat.completions.create(
                        model="requested-model", messages=[{"role": "user", "content": "hi"}],
                    )
                assert caught.value is error
            else:
                client.chat.completions.create(
                    model="requested-model", messages=[{"role": "user", "content": "hi"}],
                )
            # Must finish before client close; force_flush cannot finish an active span.
            assert provider.force_flush()
            spans = exporter.get_finished_spans()
            assert len(spans) == 1
            span = spans[0]
            assert span.attributes["gen_ai.provider.name"] == "baseten"
            assert span.status.status_code == (
                StatusCode.ERROR if interrupt else StatusCode.OK
            )
            if interrupt:
                if hide_errors:
                    assert "synthetic request interruption" not in span.to_json()
                else:
                    assert sum(event.name == "exception" for event in span.events) == 1
        assert exporter.get_finished_spans() == spans
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
