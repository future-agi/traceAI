"""Factory-created clients must use the same native transport span."""

import httpx
import pytest
from opentelemetry import trace

from .test_framework_a2a import CURRENT, URL, request, response
from .test_framework_a2a import tracing as tracing  # noqa: PLC0414

pytestmark = pytest.mark.skipif(not CURRENT, reason="SDK 1.x factory")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("early_close", [False, True])
async def test_factory_client_span_lifetime(tracing, streaming, early_close):
    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import AgentCard

    provider, exporter = tracing

    async def handle(req):
        return response(req, streaming=streaming)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        card = AgentCard(
            supported_interfaces=[
                {"url": URL, "protocol_binding": "JSONRPC", "protocol_version": "1.0"}
            ]
        )
        card.capabilities.streaming = streaming
        client = ClientFactory(
            ClientConfig(httpx_client=http, streaming=streaming)
        ).create(card)
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            stream = client.send_message(request())
            await stream.__anext__()
            assert trace.get_current_span() is parent
            if not early_close:
                with pytest.raises(StopAsyncIteration):
                    await stream.__anext__()
            await stream.aclose()
            assert len(exporter.get_finished_spans()) == 1
        await client.close()
    calls = [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("rpc.method")
    ]
    assert len(calls) == 1
