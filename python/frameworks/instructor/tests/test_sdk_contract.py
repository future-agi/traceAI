"""Execute the real Instructor/OpenAI boundary with an in-process HTTP transport."""

import json

import httpx
import instructor
try:
    from instructor import AsyncInstructor
except ImportError:
    from instructor.client import AsyncInstructor
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

from traceai_instructor import InstructorInstrumentor


class Person(BaseModel):
    name: str
    age: int


@pytest.fixture
def traces():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = InstructorInstrumentor()
    originals = (instructor.Instructor.create, AsyncInstructor.create, instructor.patch)
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter, instrumentor, provider
    finally:
        instrumentor.uninstrument()
        assert (instructor.Instructor.create, AsyncInstructor.create, instructor.patch) == originals
        provider.shutdown()


def response(request):
    assert request.url.path == "/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["tools"][0]["function"]["name"] == "Person"
    return httpx.Response(200, json={
        "id": "chatcmpl-offline", "object": "chat.completion", "created": 1,
        "model": "gpt-4o-mini", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function", "function": {
                    "name": "Person", "arguments": '{"name":"Ada","age":37}'}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def check_span(exporter):
    spans = exporter.get_finished_spans()
    span, = [s for s in spans if s.name.endswith(".create")]
    assert span.status.status_code == StatusCode.OK
    assert "Ada" in span.attributes["output.value"]
    assert "extract a person" in span.attributes["input.value"]
    assert span.parent is None


def test_sync_create(traces):
    exporter, _, _ = traces
    with openai.OpenAI(api_key="offline", http_client=httpx.Client(transport=httpx.MockTransport(response))) as sdk:
        client = instructor.from_openai(sdk)
        person = client.create(model="gpt-4o-mini", response_model=Person,
                               messages=[{"role": "user", "content": "extract a person"}])
        assert person.model_dump() == {"name": "Ada", "age": 37}
    check_span(exporter)


@pytest.mark.asyncio
async def test_async_create(traces):
    exporter, _, _ = traces
    async with openai.AsyncOpenAI(api_key="offline", http_client=httpx.AsyncClient(transport=httpx.MockTransport(response))) as sdk:
        client = instructor.from_openai(sdk)
        person = await client.create(model="gpt-4o-mini", response_model=Person,
                                     messages=[{"role": "user", "content": "extract a person"}])
        assert person.model_dump() == {"name": "Ada", "age": 37}
    check_span(exporter)


def test_failure_finishes_span(traces):
    exporter, _, _ = traces
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": {"message": "invalid request", "type": "invalid_request_error"}}))
    with openai.OpenAI(api_key="offline", max_retries=0, http_client=httpx.Client(transport=transport)) as sdk:
        client = instructor.from_openai(sdk)
        with pytest.raises(Exception, match="invalid request"):
            client.create(model="gpt-4o-mini", response_model=Person, max_retries=1,
                          messages=[{"role": "user", "content": "extract a person"}])
    span, = [s for s in exporter.get_finished_spans() if s.name.endswith(".create")]
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].name == "exception"


def test_content_redaction(traces):
    exporter, instrumentor, provider = traces
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    with openai.OpenAI(api_key="offline", http_client=httpx.Client(transport=httpx.MockTransport(response))) as sdk:
        result = instructor.from_openai(sdk).create(model="gpt-4o-mini", response_model=Person,
            messages=[{"role": "user", "content": "extract a person"}])
    assert result.name == "Ada"
    span, = [s for s in exporter.get_finished_spans() if s.name.endswith(".create")]
    encoded = json.dumps(dict(span.attributes))
    assert "Ada" not in encoded
    assert "extract a person" not in encoded
