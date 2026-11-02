"""High-level SDK stream helpers must finish spans when their context closes early."""
import asyncio
import json

import pytest
from opentelemetry.trace import StatusCode

from .test_stream_lifecycle import ARGS, base_url, client, http, streaming, tracing


def _response_events(_):
    response = {
        "id": "resp_synthetic", "object": "response", "created_at": 1,
        "model": "served-model", "status": "completed", "parallel_tool_calls": True,
        "tool_choice": "auto", "tools": [], "output": [],
        "usage": {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
    }
    events = [
        {"type": "response.created", "sequence_number": 0,
         "response": {**response, "status": "in_progress"}},
        {"type": "response.completed", "sequence_number": 1, "response": response},
    ]
    content = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()
    return http.Response(200, headers={"content-type": "text/event-stream"}, content=content)


def _stream_context(sdk, resource):
    if resource == "responses":
        return sdk.responses.stream(model="requested-model", input="synthetic prompt")
    completions = sdk.chat.completions
    if not hasattr(completions, "stream"):
        completions = sdk.beta.chat.completions
    return completions.stream(**ARGS)


@pytest.mark.parametrize("resource", ["chat", "responses"])
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("consumption", ["empty", "partial", "exhausted"])
def test_high_level_context_finalizes_on_early_exit(tracing, base_url, resource, use_async, consumption):
    provider, exporter = tracing
    handler = streaming if resource == "chat" else _response_events
    if use_async:
        async def run():
            async with client(handler, base_url, True) as sdk:
                async with _stream_context(sdk, resource) as stream:
                    if consumption == "partial":
                        await stream.__anext__()
                    elif consumption == "exhausted":
                        async for _ in stream:
                            pass
                assert stream._response.is_closed
        asyncio.run(run())
    else:
        with client(handler, base_url) as sdk:
            with _stream_context(sdk, resource) as stream:
                if consumption == "partial":
                    next(stream)
                elif consumption == "exhausted":
                    list(stream)
            assert stream._response.is_closed
    assert provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, "SDK closed the HTTP response without ending its span"
    assert spans[0].status.status_code == StatusCode.OK
    assert spans[0].attributes["gen_ai.provider.name"] == "baseten"
