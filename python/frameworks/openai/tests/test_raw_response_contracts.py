"""Raw-response return types must not change SDK or telemetry privacy contracts."""

import asyncio
import json
from contextlib import contextmanager

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor

PRIVATE = "synthetic-private-instructions-not-for-export"


def response_body():
    return {
        "id": "resp_raw_contract",
        "object": "response",
        "created_at": 1,
        "model": "synthetic",
        "status": "completed",
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "instructions": PRIVATE,
        "output": [],
        "error": None,
    }


@contextmanager
def tracing(instrumented=True):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()
    if instrumented:
        instrumentor.instrument(
            tracer_provider=provider, config=TraceConfig(hide_inputs=True)
        )
    try:
        yield exporter
    finally:
        if instrumented:
            instrumentor.uninstrument()
        provider.shutdown()


def client(handler, async_=False):
    http = (httpx.AsyncClient if async_ else httpx.Client)(
        transport=httpx.MockTransport(handler), trust_env=False
    )
    return (openai.AsyncOpenAI if async_ else openai.OpenAI)(
        api_key="synthetic-raw-contract-key",
        base_url="https://synthetic.invalid/v1",
        max_retries=0,
        http_client=http,
    )


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("parse_type", [str, bytes])
@pytest.mark.parametrize("body_kind", ["response", "text", "array"])
def test_scalar_parse_preserves_values_laziness_and_input_privacy(
    async_, parse_type, body_kind
):
    content = {
        "response": json.dumps(response_body()),
        "text": PRIVATE,
        "array": json.dumps([{"instructions": PRIVATE}]),
    }[body_kind].encode()
    expected = content if parse_type is bytes else content.decode()
    reads = []
    closes = []

    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(1)
            yield content

        def close(self):
            closes.append(1)

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(1)
            yield content

        async def aclose(self):
            closes.append(1)

    def handler(request):
        assert json.loads(request.content)["instructions"] == PRIVATE
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=AsyncBody() if async_ else SyncBody(),
        )

    params = {"model": "synthetic", "input": "question", "instructions": PRIVATE}
    with tracing() as exporter:
        async def run():
            async with client(handler, True) as sdk:
                async with sdk.responses.with_streaming_response.create(**params) as raw:
                    assert not reads
                    assert not exporter.get_finished_spans()
                    parsed = await raw.parse(to=parse_type)
                    assert type(parsed) is parse_type
                    assert parsed == expected
                    assert await raw.parse(to=parse_type) is parsed
                await raw.close()

        if async_:
            asyncio.run(run())
        else:
            with client(handler) as sdk:
                with sdk.responses.with_streaming_response.create(**params) as raw:
                    assert not reads
                    assert not exporter.get_finished_spans()
                    parsed = raw.parse(to=parse_type)
                    assert type(parsed) is parse_type
                    assert parsed == expected
                    assert raw.parse(to=parse_type) is parsed
                raw.close()

        assert len(reads) == 1
        assert closes
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is StatusCode.OK
        assert PRIVATE not in span.to_json()
        # Opaque scalar parses cannot safely be classified as assistant output.
        assert "output.value" not in span.attributes


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("instrumented", [False, True])
def test_legacy_text_remains_a_string_property(async_, instrumented):
    body = response_body()

    def handler(request):
        return httpx.Response(200, json=body)

    def check(raw):
        assert isinstance(raw.text, str)
        assert json.loads(raw.text) == body
        assert raw.text == raw.http_response.text
        parsed = raw.parse()
        assert parsed.instructions == PRIVATE
        assert raw.parse() is parsed

    with tracing(instrumented) as exporter:
        async def run():
            async with client(handler, True) as sdk:
                raw = await sdk.responses.with_raw_response.create(
                    model="synthetic", input="question", instructions=PRIVATE
                )
                check(raw)
                await raw.http_response.aclose()
                await raw.http_response.aclose()

        if async_:
            asyncio.run(run())
        else:
            with client(handler) as sdk:
                raw = sdk.responses.with_raw_response.create(
                    model="synthetic", input="question", instructions=PRIVATE
                )
                check(raw)
                raw.http_response.close()
                raw.http_response.close()

        spans = exporter.get_finished_spans()
        assert len(spans) == int(instrumented)
        if instrumented:
            assert spans[0].status.status_code is StatusCode.OK
            assert PRIVATE not in spans[0].to_json()
