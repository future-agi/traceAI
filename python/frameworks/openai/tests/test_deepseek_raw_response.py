"""Independent SDK-interface probes, synthetic keys and loopback OTLP only."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import openai
import pytest

source = Path(__file__).with_name("test_deepseek_route.py")
spec = importlib.util.spec_from_file_location("round3_route", source)
route = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = route
spec.loader.exec_module(route)
wire, httpx = route.wire, route.httpx


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("responses", [False, True])
def test_raw_response_interface(wire, asynchronous, streaming, responses):
    spans = wire("shared")
    params = dict(model="deepseek-reasoner", stream=streaming)
    params.update(
        dict(input="hello")
        if responses
        else dict(messages=[dict(role="user", content="hello")])
    )

    async def run():
        async with openai.AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://api.deepseek.com",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(route.respond)),
        ) as client:
            resource = client.responses if responses else client.chat.completions
            raw = await resource.with_raw_response.create(**params)
            if streaming:
                assert not spans  # The raw headers arrive before stream completion.
            result = raw.parse()
            if streaming:
                assert result is raw.parse()
            if streaming:
                async with result:
                    async for _ in result:
                        pass
            return result

    if asynchronous:
        result = asyncio.run(run())
    else:
        with openai.OpenAI(
            api_key="synthetic-key",
            base_url="https://api.deepseek.com",
            http_client=httpx.Client(transport=httpx.MockTransport(route.respond)),
        ) as client:
            resource = client.responses if responses else client.chat.completions
            raw = resource.with_raw_response.create(**params)
            if streaming:
                assert not spans
            result = raw.parse()
            if streaming:
                assert result is raw.parse()
                with result:
                    for _ in result:
                        pass
    a = route.attrs(spans)
    assert a["gen_ai.usage.output_tokens"] == 7
    assert a["deepseek.response_id"] == (
        "resp-offline" if responses else "completion-offline"
    )
    if not responses:
        assert a["deepseek.reasoning_content"] == route.REASONING


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("route_name", ["shared", "legacy_first", "shared_first"])
def test_raw_stream_failure_status(wire, asynchronous, route_name):
    spans = wire(route_name, hide_error_details=True)

    def respond(request):
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "req-synthetic",
            },
            content="data: "
            + json.dumps(
                {"error": {"message": "PRIVATE_STREAM_ERROR", "type": "server_error"}}
            )
            + "\n\n",
        )

    params = dict(
        model="deepseek-reasoner",
        messages=[dict(role="user", content="hello")],
        stream=True,
    )

    async def run():
        async with openai.AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://api.deepseek.com",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            raw = await client.chat.completions.with_raw_response.create(**params)
            assert raw.headers["x-request-id"] == "req-synthetic"
            assert not spans
            async with raw.parse() as stream:
                async for _ in stream:
                    pass

    with pytest.raises(openai.APIError, match="PRIVATE_STREAM_ERROR"):
        if asynchronous:
            asyncio.run(run())
        else:
            with openai.OpenAI(
                api_key="synthetic-key",
                base_url="https://api.deepseek.com",
                http_client=httpx.Client(transport=httpx.MockTransport(respond)),
            ) as client:
                raw = client.chat.completions.with_raw_response.create(**params)
                assert raw.headers["x-request-id"] == "req-synthetic"
                assert not spans
                with raw.parse() as stream:
                    for _ in stream:
                        pass
    assert len(spans) == 1
    assert spans[0].status.code == 2
    assert any(e.name == "exception" for e in spans[0].events)
    assert b"PRIVATE_STREAM_ERROR" not in spans[0].SerializeToString()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("parse_first", [False, True])
def test_raw_stream_close_before_exhaustion(wire, asynchronous, parse_first):
    spans = wire("shared")
    params = dict(
        model="deepseek-reasoner",
        messages=[dict(role="user", content="hello")],
        stream=True,
    )

    async def run():
        async with openai.AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://api.deepseek.com",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(route.respond)),
        ) as client:
            raw = await client.chat.completions.with_raw_response.create(**params)
            assert not spans
            if parse_first:
                stream = raw.parse()
                await stream.__anext__()
            await raw.aclose()
            await raw.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        with openai.OpenAI(
            api_key="synthetic-key",
            base_url="https://api.deepseek.com",
            http_client=httpx.Client(transport=httpx.MockTransport(route.respond)),
        ) as client:
            raw = client.chat.completions.with_raw_response.create(**params)
            assert not spans
            if parse_first:
                stream = raw.parse()
                next(stream)
            raw.close()
            raw.close()
    assert len(spans) == 1
    assert spans[0].status.code == 1
