import importlib.util
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from .test_framework_a2a import CURRENT, response


@pytest.mark.asyncio
@pytest.mark.skipif(not CURRENT, reason="documented SDK 1.3 example")
async def test_example_uses_real_discovery_and_client_factory(monkeypatch):
    path = Path(__file__).parents[1] / "examples" / "multi_agent_demo.py"
    spec = importlib.util.spec_from_file_location("a2a_example", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(example, "register", lambda **kwargs: provider)
    monkeypatch.setenv("A2A_AGENT_URL", "https://offline.invalid")
    requests = []

    async def handle(req):
        requests.append(req)
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "name": "synthetic-agent",
                    "supportedInterfaces": [
                        {
                            "url": "https://offline.invalid",
                            "protocolBinding": "JSONRPC",
                            "protocolVersion": "1.0",
                        }
                    ],
                },
            )
        return response(req)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(example.httpx, "AsyncClient", lambda: http)
    await example.main()
    assert [req.method for req in requests] == ["GET", "POST"]
    calls = [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("rpc.method") == "send_message"
    ]
    assert len(calls) == 1
    assert requests[1].headers["traceparent"].split("-")[2] == format(
        calls[0].context.span_id, "016x"
    )
