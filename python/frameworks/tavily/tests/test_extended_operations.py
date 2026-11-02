"""Real Tavily clients with fake HTTP transports, never fake SDK methods."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import requests
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from tavily import AsyncTavilyClient, TavilyClient

from _support import attrs, instrumented
from _tavily_fake import TAVILY_KEY

REQUEST_ID = "request-1234"
PRIVATE_URL = "https://example.com/private?token=secret-url"
PRIVATE_OUTPUT = "private-research-output"
QUESTION = "Research this private question"
CHUNKS = [
    b'event: start\ndata: {"request_id":"request-1234"}\n\n',
    b'event: content\ndata: {"content":"private-research-output"}\n\n',
]
METHODS = ("map", "crawl", "research", "get_research")


class Transport:
    def __init__(self):
        self.calls = []
        self.contexts = []
        self.closed = False
        self.chunks = CHUNKS
        self.error = None
        self.status = 200
        self.wait = None

    def payload(self, method):
        if self.status != 200:
            return {"detail": {"error": f"{QUESTION} {PRIVATE_URL} {TAVILY_KEY} {PRIVATE_OUTPUT}"}}
        if method in ("search", "map", "crawl"):
            return {"request_id": REQUEST_ID, "results": [PRIVATE_URL, PRIVATE_URL]}
        return {"request_id": REQUEST_ID, "status": "completed", "content": PRIVATE_OUTPUT}

    def record(self, method, body):
        self.calls.append((method, body))
        self.contexts.append(trace.get_current_span().get_span_context())

    def sync_send(self, request, **kwargs):
        method = request.url.split("/")[3]
        body = json.loads(request.body) if request.body else None
        self.record(method, body)
        response = requests.Response()
        response.status_code = self.status
        response.url = request.url
        response.request = request
        if body and body.get("stream") and self.status == 200:
            response.raw = self
        else:
            response._content = json.dumps(self.payload(method)).encode()
        return response

    def stream(self, *args, **kwargs):
        for chunk in self.chunks:
            self.contexts.append(trace.get_current_span().get_span_context())
            yield chunk
        if self.error:
            raise self.error

    def close(self):
        self.closed = True

    def release_conn(self):
        self.closed = True

    async def async_send(self, request):
        method = request.url.path.split("/")[1]
        body = json.loads(request.content) if request.content else None
        self.record(method, body)
        owner = self

        class Bytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in owner.chunks:
                    owner.contexts.append(trace.get_current_span().get_span_context())
                    yield chunk
                if owner.wait is not None:
                    await owner.wait.wait()
                if owner.error:
                    raise owner.error

            async def aclose(self):
                owner.closed = True

        if body and body.get("stream") and self.status == 200:
            return httpx.Response(200, stream=Bytes())
        return httpx.Response(self.status, json=self.payload(method))

    def sync_client(self, monkeypatch):
        client = TavilyClient(api_key=TAVILY_KEY)
        monkeypatch.setattr(client.session, "send", self.sync_send)
        return client

    async def async_client(self):
        client = AsyncTavilyClient(api_key=TAVILY_KEY)
        await client.close()
        client._client = httpx.AsyncClient(
            base_url="https://api.tavily.com", headers={"Authorization": f"Bearer {TAVILY_KEY}"},
            transport=httpx.MockTransport(self.async_send),
        )
        return client


def argument(method):
    return QUESTION if method == "research" else REQUEST_ID if method == "get_research" else PRIVATE_URL


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("is_async", [False, True])
def test_extended_operations_trace_once_with_real_sdk(monkeypatch, method, is_async):
    transport = Transport()

    async def call_async():
        client = await transport.async_client()
        try:
            return await getattr(client, method)(argument(method))
        finally:
            await client.close()

    with instrumented() as traced:
        with traced.provider.get_tracer("test").start_as_current_span("parent") as parent:
            if is_async:
                result = asyncio.run(call_async())
            else:
                result = getattr(transport.sync_client(monkeypatch), method)(argument(method))
        children = [span for span in traced.spans() if span.name != "parent"]
        assert len(children) == 1
        span = children[0]
        assert span.name == f"tavily.{method}"
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.status.status_code == StatusCode.OK
        assert attrs(span)["gen_ai.span.kind"] == "TOOL"
        assert attrs(span)["tavily.request_id"] == REQUEST_ID
        if method in ("map", "crawl"):
            assert attrs(span)["tavily.result_count"] == 2
            assert attrs(span)["tavily.url_count"] == 1
        assert result == transport.payload(method)
        assert len(transport.calls) == 1
        assert transport.contexts[0].span_id == span.context.span_id
        for value in (TAVILY_KEY, PRIVATE_URL, PRIVATE_OUTPUT):
            assert value not in traced.wire()


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("is_async", [False, True])
def test_errors_export_only_type_but_keep_original_sdk_exception(monkeypatch, method, is_async):
    transport = Transport()
    transport.status = 400

    async def call_async():
        client = await transport.async_client()
        try:
            return await getattr(client, method)(argument(method))
        finally:
            await client.close()

    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        with pytest.raises(Exception) as caught:
            if is_async:
                asyncio.run(call_async())
            else:
                getattr(transport.sync_client(monkeypatch), method)(argument(method))
        assert PRIVATE_OUTPUT in str(caught.value)
        span = traced.one()
        assert span.status.status_code == StatusCode.ERROR
        # Current core derives status descriptions from exception.message;
        # this type-only event has none. Legacy core retains our type summary.
        expected = None if hasattr(TraceConfig(), "hide_error_details") else type(caught.value).__name__
        assert span.status.description == expected
        assert len(span.events) == 1
        assert dict(span.events[0].attributes) == {"exception.type": type(caught.value).__name__}
        for private in (QUESTION, TAVILY_KEY, PRIVATE_URL, PRIVATE_OUTPUT, REQUEST_ID):
            assert private not in traced.wire()


@pytest.mark.parametrize("identifier", [TAVILY_KEY, PRIVATE_URL, "x" * 129, {"bad": "shape"}])
def test_untrusted_request_identifiers_are_not_exported(monkeypatch, identifier):
    transport = Transport()
    transport.payload = lambda method: {"request_id": identifier}
    with instrumented() as traced:
        response = transport.sync_client(monkeypatch).research(QUESTION)
        assert response["request_id"] == identifier
        assert "tavily.request_id" not in attrs(traced.one())


def test_research_prompt_redacts_keys_and_pii(monkeypatch):
    transport = Transport()
    with instrumented(config=TraceConfig(pii_redaction=True)) as traced:
        transport.sync_client(monkeypatch).research(f"contact jane@example.com {TAVILY_KEY}")
        value = attrs(traced.one())["input.value"]
        assert "jane@example.com" not in value
        assert TAVILY_KEY not in value
        assert "<EMAIL_ADDRESS>" in value
        assert "[redacted]" in value
