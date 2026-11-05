"""Pin the documented WebSocket boundary: HTTP tracing is not WS tracing."""
import asyncio
import importlib
import json

import pytest

from .test_audio_transcription import sdk, wire
from .test_openai_compatible_spans import responses_body


@pytest.mark.parametrize("use_async", [False, True])
def test_responses_websocket_is_outside_http_instrumentation(wire, monkeypatch, use_async):
    resources = importlib.import_module("openai.resources.responses.responses")
    if not hasattr(resources.Responses, "connect"):
        pytest.skip("Responses WebSockets are not available in this SDK")
    ws_client = pytest.importorskip("websockets.sync.client")
    ws_helpers = importlib.import_module("openai.lib._websocket")
    payloads = wire()
    sent = []
    closed = []
    event = json.dumps({"type": "response.completed", "sequence_number": 0,
                        "response": responses_body()}).encode()

    class Connection:
        def send(self, data):
            sent.append(json.loads(data))

        def recv(self, **kwargs):
            return event

        def close(self, **kwargs):
            closed.append(True)

    class AsyncConnection(Connection):
        async def send(self, data):
            super().send(data)

        async def recv(self, **kwargs):
            return super().recv(**kwargs)

        async def close(self, **kwargs):
            super().close(**kwargs)

    def connect(url, **kwargs):
        assert url == "wss://api.openai.com/v1/responses"
        return Connection()

    async def async_connect(url, **kwargs):
        assert url == "wss://api.openai.com/v1/responses"
        return AsyncConnection()

    monkeypatch.setattr(ws_client, "connect", connect)
    monkeypatch.setattr(ws_helpers, "_WebSocketConnect", async_connect)

    def reject_http(request):
        pytest.fail("responses.connect unexpectedly used the HTTP transport")

    async def run_async():
        async with sdk(reject_http, True) as client:
            async with client.responses.connect() as connection:
                await connection.response.create(model="synthetic-model", input="hello")
                result = await connection.recv()
                assert result.type == "response.completed"
                assert result.response.usage.total_tokens == 7

    if use_async:
        asyncio.run(run_async())
    else:
        with sdk(reject_http, False) as client:
            with client.responses.connect() as connection:
                connection.response.create(model="synthetic-model", input="hello")
                result = connection.recv()
                assert result.type == "response.completed"
                assert result.response.usage.total_tokens == 7
    assert len(sent) == 1 and sent[0]["type"] == "response.create"
    assert closed == [True]
    assert not payloads  # Document the gap; do not advertise unsupported WS traces.
