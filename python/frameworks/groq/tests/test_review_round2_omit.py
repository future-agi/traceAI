"""Verify emitted request parameters represent on-wire values only."""

import json

import httpx
import pytest
import test_stream_lifecycle as h
from groq import AsyncGroq, Groq

COMPLETION = {
    "id": "synthetic",
    "object": "chat.completion",
    "created": 0,
    "model": "response-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "answer"},
            "finish_reason": "stop",
        }
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_default_omissions_not_exported(asynchronous):
    wire = []

    def handler(req):
        wire.append(json.loads(req.content))
        return httpx.Response(200, json=COMPLETION)

    with h.instrumented() as (memory, provider):
        params = {
            "model": "synthetic-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        if asynchronous:
            async with AsyncGroq(
                api_key="synthetic",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ) as client:
                await client.chat.completions.create(**params)
        else:
            with Groq(
                api_key="synthetic",
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            ) as client:
                client.chat.completions.create(**params)
        (span,) = memory.get_finished_spans()
        exported = json.loads(span.attributes["gen_ai.request.parameters"])
        assert "top_p" not in wire[0]
        assert "top_p" not in exported, "wire omitted top_p but trace recorded " + str(
            exported["top_p"]
        )
        assert "tools" not in exported, "wire omitted tools but trace recorded " + str(
            exported["tools"]
        )
