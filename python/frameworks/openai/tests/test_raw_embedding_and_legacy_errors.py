"""Endpoint policy and buffered legacy lifetime must survive explicit parse types."""

import asyncio
import json
from typing import Any, Dict

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode
from traceai_openai import set_client_provider

from .test_raw_response_contracts import client, tracing


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "parse_type",
    ["default", None, openai.types.CreateEmbeddingResponse, str, bytes,
     Dict[str, Any], httpx.Response],
)
@pytest.mark.parametrize("policy", ["hide_vectors", "hide_outputs", "default"])
def test_embedding_parse_types_preserve_values_and_privacy(async_, legacy, parse_type, policy):
    vector = [0.123456789, 0.987654321]
    body = {
        "object": "list",
        "model": "text-embedding-synthetic",
        "data": [{"object": "embedding", "index": 0, "embedding": vector}],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }
    content = json.dumps(body).encode()
    reads = []
    closes = []

    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(1)
            yield content

        def close(self):
            closes.append(1)

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(1)
            yield content

        async def aclose(self):
            closes.append(1)

    def handler(request):
        assert request.url.path == "/v1/embeddings"
        assert json.loads(request.content)["encoding_format"] == "float"
        return httpx.Response(
            200, headers={"content-type": "application/json"},
            stream=AsyncBody() if async_ else SyncBody(),
        )

    def check_value(parsed):
        if parse_type in ("default", None, openai.types.CreateEmbeddingResponse):
            assert isinstance(parsed, openai.types.CreateEmbeddingResponse)
            assert parsed.data[0].embedding == vector
        elif parse_type is str:
            assert type(parsed) is str
            assert parsed == content.decode()
        elif parse_type is bytes:
            assert type(parsed) is bytes
            assert parsed == content
        elif parse_type is httpx.Response:
            assert isinstance(parsed, httpx.Response)
            assert parsed.content == content
        else:
            assert type(parsed) is dict
            assert parsed == body

    def parse_legacy(raw):
        if parse_type is bytes:
            # Unlike modern APIResponse, LegacyAPIResponse rejects bytes.
            with pytest.raises(RuntimeError, match="Unsupported type"):
                raw.parse(to=bytes)
        else:
            parsed = raw.parse(**parse_kwargs)
            check_value(parsed)
            assert raw.parse(**parse_kwargs) is parsed

    config = TraceConfig(
        hide_embedding_vectors=policy == "hide_vectors", hide_outputs=policy == "hide_outputs"
    )
    params = dict(model=body["model"], input="safe", encoding_format="float")
    parse_kwargs = {} if parse_type == "default" else {"to": parse_type}
    with tracing(config=config) as exporter:
        async def run():
            async with client(handler, True) as sdk:
                set_client_provider(sdk, provider="openai")
                if legacy:
                    raw = await sdk.embeddings.with_raw_response.create(**params)
                    parse_legacy(raw)
                else:
                    async with sdk.embeddings.with_streaming_response.create(**params) as raw:
                        assert not reads
                        assert not exporter.get_finished_spans()
                        parsed = await raw.parse(**parse_kwargs)
                        check_value(parsed)
                        assert await raw.parse(**parse_kwargs) is parsed
                    await raw.close()

        if async_:
            asyncio.run(run())
        else:
            with client(handler) as sdk:
                set_client_provider(sdk, provider="openai")
                if legacy:
                    raw = sdk.embeddings.with_raw_response.create(**params)
                    parse_legacy(raw)
                else:
                    with sdk.embeddings.with_streaming_response.create(**params) as raw:
                        assert not reads
                        assert not exporter.get_finished_spans()
                        parsed = raw.parse(**parse_kwargs)
                        check_value(parsed)
                        assert raw.parse(**parse_kwargs) is parsed
                    raw.close()

        assert reads == [1]
        assert closes
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is StatusCode.OK
        assert span.attributes["gen_ai.span.kind"] == "EMBEDDING"
        if policy != "default":
            assert all(str(value) not in span.to_json() for value in vector)
        if policy != "hide_outputs":
            if legacy or parse_type in ("default", None, openai.types.CreateEmbeddingResponse, Dict[str, Any]):
                assert json.loads(span.attributes["output.value"]) == {
                    "model": body["model"], "embeddings": 1, "dimensions": 2,
                }
            else:
                assert "output.value" not in span.attributes


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("body", [b'{"truncated":', b""])
@pytest.mark.parametrize("consumer", ["content", "text"])
@pytest.mark.parametrize("later_parse", [False, True])
@pytest.mark.parametrize("instrumented", [False, True])
def test_malformed_legacy_finishes_without_requiring_parse_or_close(
    async_, body, consumer, later_parse, instrumented
):
    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with tracing(instrumented) as exporter:
        def check(raw):
            expected = body if consumer == "content" else body.decode()
            assert getattr(raw, consumer) == expected
            # Buffered legacy envelopes have no native close() requirement.
            assert len(exporter.get_finished_spans()) == int(instrumented)
            if later_parse:
                # Telemetry's optional failed parse must not replace SDK behavior.
                for _ in range(2):
                    with pytest.raises(json.JSONDecodeError):
                        raw.parse()
                assert raw.parse(to=str) == body.decode()
                with pytest.raises(RuntimeError, match="Unsupported type"):
                    raw.parse(to=bytes)
                assert getattr(raw, consumer) == expected

        async def run():
            async with client(handler, True) as sdk:
                set_client_provider(sdk, provider="openai")
                raw = await sdk.responses.with_raw_response.create(model="synthetic", input="safe")
                check(raw)

        if async_:
            asyncio.run(run())
        else:
            with client(handler) as sdk:
                set_client_provider(sdk, provider="openai")
                raw = sdk.responses.with_raw_response.create(model="synthetic", input="safe")
                check(raw)

        spans = exporter.get_finished_spans()
        assert len(spans) == int(instrumented)
        if instrumented:
            assert spans[0].status.status_code is StatusCode.UNSET
            assert "output.value" not in spans[0].attributes
            assert not spans[0].events
