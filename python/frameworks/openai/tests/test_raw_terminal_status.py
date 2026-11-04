"""Terminal generation status survives scalar parsing and unbuffered body readers."""

import asyncio
import json

import httpx
import pytest
from opentelemetry.trace import StatusCode

from .test_raw_response_contracts import PRIVATE, client, response_body, tracing


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("method", ["parse_str", "parse_bytes", "iter_bytes", "iter_text", "iter_lines"])
@pytest.mark.parametrize("status", ["completed", "failed", "incomplete", "cancelled"])
def test_unbuffered_terminal_status(async_, method, status):
    body = response_body()
    body["status"] = status
    payload = json.dumps(body, indent=2).encode()
    reads = []
    closes = []

    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(payload), 7):
                reads.append(offset)
                yield payload[offset:offset + 7]

        def close(self):
            closes.append(1)

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in SyncBody():
                yield chunk

        async def aclose(self):
            closes.append(1)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=AsyncBody() if async_ else SyncBody(),
        )

    params = {"model": "synthetic", "input": "question", "instructions": PRIVATE}
    parse_type = str if method == "parse_str" else bytes

    def check_result(result):
        if method == "parse_str":
            assert type(result) is str and result == payload.decode()
        elif method == "parse_bytes":
            assert type(result) is bytes and result == payload
        elif method == "iter_bytes":
            assert result == [payload[offset:offset + 7] for offset in range(0, len(payload), 7)]
        elif method == "iter_text":
            assert result == [payload[offset:offset + 7].decode() for offset in range(0, len(payload), 7)]
        else:
            assert result == payload.decode().splitlines()

    with tracing() as exporter:
        async def run():
            async with client(handler, True) as sdk:
                async with sdk.responses.with_streaming_response.create(**params) as raw:
                    assert not reads and not exporter.get_finished_spans()
                    if method.startswith("parse"):
                        result = await raw.parse(to=parse_type)
                        assert await raw.parse(to=parse_type) is result
                    else:
                        result = [chunk async for chunk in getattr(raw, method)()]
                        with pytest.raises(httpx.ResponseNotRead):
                            _ = raw.http_response.content
                    check_result(result)
                await raw.close()

        if async_:
            asyncio.run(run())
        else:
            with client(handler) as sdk:
                with sdk.responses.with_streaming_response.create(**params) as raw:
                    assert not reads and not exporter.get_finished_spans()
                    if method.startswith("parse"):
                        result = raw.parse(to=parse_type)
                        assert raw.parse(to=parse_type) is result
                    else:
                        result = list(getattr(raw, method)())
                        with pytest.raises(httpx.ResponseNotRead):
                            _ = raw.http_response.content
                    check_result(result)
                raw.close()

        assert reads and closes
        (span,) = exporter.get_finished_spans()
        assert PRIVATE not in span.to_json()
        assert "output.value" not in span.attributes
        assert span.status.status_code is (
            StatusCode.OK if status == "completed" else StatusCode.ERROR
        )


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("method", ["iter_bytes", "iter_text", "iter_lines"])
@pytest.mark.parametrize("outcome", ["opaque", "truncated", "oversized", "unread", "partial", "error", "cancel", "sse"])
def test_raw_iterator_unknown_and_interrupted_consumption(async_, method, outcome):
    reads = []
    closes = []
    error = KeyboardInterrupt("synthetic cancel") if outcome == "cancel" else RuntimeError("synthetic failure")
    payload = {
        "opaque": PRIVATE.encode(),
        "truncated": b'{"status": "completed",',
        "oversized": json.dumps({"instructions": PRIVATE * 3000, "status": "failed"}).encode(),
    }.get(outcome, b'{\n"status": "completed"\n}\n')

    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(1)
            # The newline lets even the line reader yield before an early exit.
            yield payload[:2]
            if outcome in {"error", "cancel"}:
                raise error
            yield payload[2:]

        def close(self):
            closes.append(1)

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in SyncBody():
                yield chunk

        async def aclose(self):
            closes.append(1)

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"},
            stream=AsyncBody() if async_ else SyncBody(),
        )

    params = {"model": "synthetic", "input": "question", "stream": outcome == "sse"}
    with tracing() as exporter:
        async def run():
            async with client(handler, True) as sdk:
                async with sdk.responses.with_streaming_response.create(**params) as raw:
                    iterator = getattr(raw, method)()
                    assert not reads and not exporter.get_finished_spans()
                    if outcome == "partial":
                        await iterator.__anext__()
                    elif outcome != "unread":
                        assert [chunk async for chunk in iterator]
                await raw.close()
                await iterator.aclose()

        def sync():
            with client(handler) as sdk:
                with sdk.responses.with_streaming_response.create(**params) as raw:
                    iterator = getattr(raw, method)()
                    assert not reads and not exporter.get_finished_spans()
                    if outcome == "partial":
                        next(iterator)
                    elif outcome != "unread":
                        assert list(iterator)
                raw.close()
                iterator.close()

        if outcome in {"error", "cancel"}:
            with pytest.raises(type(error)) as caught:
                asyncio.run(run()) if async_ else sync()
            assert caught.value is error
        else:
            asyncio.run(run()) if async_ else sync()
        assert len(reads) == int(outcome != "unread")
        assert closes
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is (
            StatusCode.UNSET if outcome in {"opaque", "truncated", "oversized"} else StatusCode.ERROR
        )
        assert PRIVATE not in span.to_json()
        assert "output.value" not in span.attributes
        if outcome in {"error", "cancel"}:
            assert len([event for event in span.events if event.name == "exception"]) == 1


def test_raw_body_inspection_is_bounded_and_discards_consumed_content():
    from traceai_openai._stream import _RawBodyBuffer

    buffer = _RawBodyBuffer()
    buffer.add(b'{"status": "failed"}')
    assert buffer.take() == {"status": "failed"}
    assert buffer._body is None
    for chunk in (b"x" * 1024, "é" * 1024):
        buffer = _RawBodyBuffer()
        for _ in range(200):
            buffer.add(chunk)
            assert buffer._body is None or len(buffer._body) <= buffer._LIMIT
        assert buffer.take() is None


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("parse_type", [str, bytes])
def test_scalar_parse_does_not_repeat_sdk_parse_or_post_parser(async_, parse_type):
    body = response_body()
    body["status"] = "failed"
    calls = []
    post_parses = []

    def handler(request):
        return httpx.Response(200, json=body)

    def post_parser(parsed):
        post_parses.append(parsed)
        return parsed

    with tracing() as exporter:
        async def run():
            async with client(handler, True) as sdk:
                async with sdk.responses.with_streaming_response.create(model="synthetic", input="question") as raw:
                    raw.__wrapped__._options.post_parser = post_parser
                    original = raw.__wrapped__.parse

                    async def parse(*args, **kwargs):
                        calls.append(1)
                        return await original(*args, **kwargs)

                    raw.__wrapped__.parse = parse
                    parsed = await raw.parse(to=parse_type)
                    assert await raw.parse(to=parse_type) is parsed
                    assert json.loads(parsed) == body

        if async_:
            asyncio.run(run())
        else:
            with client(handler) as sdk:
                with sdk.responses.with_streaming_response.create(model="synthetic", input="question") as raw:
                    raw.__wrapped__._options.post_parser = post_parser
                    original = raw.__wrapped__.parse

                    def parse(*args, **kwargs):
                        calls.append(1)
                        return original(*args, **kwargs)

                    raw.__wrapped__.parse = parse
                    parsed = raw.parse(to=parse_type)
                    assert raw.parse(to=parse_type) is parsed
                    assert json.loads(parsed) == body
        assert len(calls) == 2  # Exactly the application's two explicit calls.
        assert len(post_parses) == 1  # The second call is served from the SDK cache.
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert PRIVATE not in span.to_json()
