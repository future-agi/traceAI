"""Buffered and lazy raw-response contracts across the supported OpenAI SDKs."""

import asyncio
import gc
import inspect
import json

import httpx
import openai
import pytest
from opentelemetry.trace import StatusCode

from .test_privacy_and_raw_streams import configured_tracing
from .test_privacy_regressions import ANSWER, COMPLETION, MESSAGES, SECRET, USAGE
from .test_sdk_lifecycle import BASE_URL, MODEL, TOKEN


class SyncJSONBody(httpx.SyncByteStream):
    def __init__(self, invalid=False):
        self.reads = 0
        self.closes = 0
        self.data = b"not-json" if invalid else json.dumps(COMPLETION).encode()

    def __iter__(self):
        self.reads += 1
        yield self.data

    def close(self):
        self.closes += 1


class AsyncJSONBody(httpx.AsyncByteStream):
    def __init__(self, invalid=False):
        self.body = SyncJSONBody(invalid)

    async def __aiter__(self):
        for data in self.body:
            yield data

    async def aclose(self):
        self.body.close()


def mock_transport(body):
    return httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "application/json", "x-request-id": "synthetic"},
        stream=body,
    ))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("invalid", [False, True])
def test_buffered_raw_finishes_without_changing_public_api(asynchronous, invalid):
    body = AsyncJSONBody(invalid) if asynchronous else SyncJSONBody(invalid)
    counts = body.body if asynchronous else body
    params = {"model": MODEL, "messages": MESSAGES, "stream": False,
              "extra_headers": {"credential": SECRET}}

    def check(raw, exporter):
        assert counts.reads == 1
        assert len(exporter.get_finished_spans()) == 1
        attrs = exporter.get_finished_spans()[0].attributes
        assert SECRET not in str(attrs)
        assert TOKEN not in str(attrs)
        assert raw.status_code == 200
        assert raw.headers["x-request-id"] == "synthetic"
        assert raw.http_response.content == counts.data
        if not invalid:
            assert attrs["gen_ai.usage.total_tokens"] == USAGE["total_tokens"]
            assert ANSWER in attrs["output.value"]
        else:
            assert "output.value" not in attrs

    with configured_tracing() as exporter:
        if asynchronous:
            async def run():
                async with openai.AsyncOpenAI(
                    api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                    http_client=httpx.AsyncClient(transport=mock_transport(body)),
                ) as client:
                    raw = await client.chat.completions.with_raw_response.create(**params)
                    check(raw, exporter)
                    async def parse():
                        result = raw.parse()
                        return await result if inspect.isawaitable(result) else result
                    if invalid:
                        with pytest.raises(json.JSONDecodeError):
                            await parse()
                    else:
                        parsed = await parse()
                        assert await parse() is parsed
                        assert parsed.choices[0].message.content == ANSWER
            asyncio.run(run())
        else:
            with openai.OpenAI(
                api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                http_client=httpx.Client(transport=mock_transport(body)),
            ) as client:
                raw = client.chat.completions.with_raw_response.create(**params)
                check(raw, exporter)
                if invalid:
                    with pytest.raises(json.JSONDecodeError):
                        raw.parse()
                else:
                    assert raw.parse() is raw.parse()
                    assert raw.parse().choices[0].message.content == ANSWER
        gc.collect()
        assert len(exporter.get_finished_spans()) == 1
    assert counts.reads == counts.closes == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["parse", "unparsed", "invalid"])
@pytest.mark.parametrize("hide", [False, True])
def test_lazy_raw_nonstream_preserves_reads_cache_close_and_privacy(asynchronous, mode, hide):
    body = AsyncJSONBody(mode == "invalid") if asynchronous else SyncJSONBody(mode == "invalid")
    counts = body.body if asynchronous else body
    params = {"model": MODEL, "messages": MESSAGES, "stream": False,
              "extra_query": {"credential": SECRET}}
    with configured_tracing(hide_output_text=hide) as exporter:
        if asynchronous:
            async def run():
                async with openai.AsyncOpenAI(
                    api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                    http_client=httpx.AsyncClient(transport=mock_transport(body)),
                ) as client:
                    if not hasattr(client.chat.completions, "with_streaming_response"):
                        pytest.skip("Lazy raw response API was added after OpenAI 1.0")
                    async with client.chat.completions.with_streaming_response.create(**params) as raw:
                        assert raw.status_code == 200
                        assert counts.reads == 0
                        assert not exporter.get_finished_spans()
                        if mode == "parse":
                            parsed = await raw.parse()
                            assert await raw.parse() is parsed
                            assert parsed.choices[0].message.content == ANSWER
                        elif mode == "invalid":
                            with pytest.raises(json.JSONDecodeError):
                                await raw.parse()
            asyncio.run(run())
        else:
            with openai.OpenAI(
                api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                http_client=httpx.Client(transport=mock_transport(body)),
            ) as client:
                if not hasattr(client.chat.completions, "with_streaming_response"):
                    pytest.skip("Lazy raw response API was added after OpenAI 1.0")
                with client.chat.completions.with_streaming_response.create(**params) as raw:
                    assert raw.status_code == 200
                    assert counts.reads == 0
                    assert not exporter.get_finished_spans()
                    if mode == "parse":
                        parsed = raw.parse()
                        assert raw.parse() is parsed
                        assert parsed.choices[0].message.content == ANSWER
                    elif mode == "invalid":
                        with pytest.raises(json.JSONDecodeError):
                            raw.parse()
        gc.collect()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code == (StatusCode.ERROR if mode == "invalid" else StatusCode.OK)
        attrs = span.attributes
        assert SECRET not in str(attrs)
        assert TOKEN not in str(attrs)
        if mode == "parse":
            assert attrs["gen_ai.usage.total_tokens"] == 5
            assert (ANSWER in attrs["output.value"]) is not hide
        else:
            assert "output.value" not in attrs
    assert counts.reads == (mode != "unparsed")
    assert counts.closes == 1
