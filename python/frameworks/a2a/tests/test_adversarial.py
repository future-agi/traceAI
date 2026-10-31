import json
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.sdk.trace import Tracer
from traceai_a2a import A2AInstrumentor
from traceai_a2a import _native as native

from .test_framework_a2a import CURRENT, MODERN, URL, client, request, response
from .test_framework_a2a import tracing as tracing  # noqa: PLC0414


@pytest.mark.asyncio
async def test_provider_failure_does_not_break_request(tracing, monkeypatch, caplog):
    _, exporter = tracing

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(Tracer, "start_span", fail)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        assert await client(http).send_message(request()) is not None
    assert not exporter.get_finished_spans()
    assert "Could not start native A2A span" in caplog.text


def test_unsupported_sdk_does_not_report_success(monkeypatch):
    monkeypatch.setattr(native, "import_module", lambda name: SimpleNamespace())
    with pytest.raises(RuntimeError, match="No supported"):
        A2AInstrumentor().instrument()
    assert not A2AInstrumentor().is_instrumented_by_opentelemetry


@pytest.mark.asyncio
@pytest.mark.skipif(CURRENT or not MODERN, reason="0.3 deprecated facade")
async def test_legacy_facade_delegates_without_duplicate_spans(tracing):
    from a2a.client import A2AClient
    from a2a.types import SendMessageRequest

    _, exporter = tracing
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        with pytest.warns(DeprecationWarning):
            facade = A2AClient(http, url=URL)
        await facade.send_message(SendMessageRequest(id="request-id", params=request()))
    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.a2a.task.state"] == "completed"


@pytest.mark.asyncio
@pytest.mark.skipif(not CURRENT, reason="SDK 1.x protocol compatibility transport")
@pytest.mark.parametrize("streaming", [False, True])
async def test_compat_jsonrpc_native_operation(tracing, streaming):
    from a2a.compat.v0_3.jsonrpc_transport import CompatJsonRpcTransport
    from a2a.types import AgentCard

    _, exporter = tracing
    captured = []

    async def handle(req):
        captured.append(req.headers)
        body = {
            "jsonrpc": "2.0",
            "id": json.loads(req.content)["id"],
            "result": {
                "kind": "task",
                "id": "task-id",
                "contextId": "context-id",
                "status": {"state": "completed"},
            },
        }
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(body)}\n\n",
            )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        transport = CompatJsonRpcTransport(http, AgentCard(), URL)
        if streaming:
            stream = transport.send_message_streaming(request())
            assert not exporter.get_finished_spans()
            async for _ in stream:
                pass
        else:
            await transport.send_message(request())
    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.a2a.task.state"] == "completed"
    assert captured[0]["traceparent"].split("-")[2] == format(
        span.context.span_id, "016x"
    )
