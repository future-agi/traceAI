"""Qwen uses the OpenAI client; Hong Kong must keep DashScope attribution.

Official shared endpoint: https://www.alibabacloud.com/help/en/model-studio/base-url
Provider traffic is mocked; OTLP exports use the loopback receiver shared with
Parallel's contracts (upstream protobuf exporter and test-only OTLP JSON encoder).
"""

import asyncio
import json

import httpx
import openai
import pytest

from traceai_openai._providers import provider_from_host

from .test_openai_compatible_spans import (
    ANSWER,
    REQUEST_MODEL,
    RESPONSE_MODEL,
    async_client,
    chat_completion,
    chat_stream,
    client,
    unauthorized,
)
from .test_parallel_provider import wire_tracer  # noqa: F401 -- shared pytest fixture
from .test_vercel_gateway import collector  # noqa: F401 -- shared pytest fixture

BASE_URL = "https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1"
PROMPT = "synthetic-qwen-private-question"


@pytest.mark.parametrize("host,expected", [
    ("cn-hongkong.dashscope.aliyuncs.com", "dashscope"),
    ("CN-HONGKONG.DASHSCOPE.ALIYUNCS.COM.", "dashscope"),
    ("dashscope.aliyuncs.com", "dashscope"),
    ("dashscope-intl.aliyuncs.com", "dashscope"),
    ("dashscope-us.aliyuncs.com", "dashscope"),
    ("workspace.cn-hongkong.maas.aliyuncs.com", "dashscope"),
    ("cn-hongkong.dashscope.aliyuncs.com.customer.example", "openai"),
    ("proxy.cn-hongkong.dashscope.aliyuncs.com", "openai"),
    ("notcn-hongkong.dashscope.aliyuncs.com", "openai"),
    ("unlisted.dashscope.aliyuncs.com", "openai"),
    ("notmaas.aliyuncs.com", "openai"),
    ("workspace.cn-hongkong.maas.aliyuncs.com.customer.example", "openai"),
    ("localhost", "openai"),
])
def test_qwen_host_boundaries(host, expected):
    assert provider_from_host(host) == expected


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compress", [False, True], ids=["plain", "gzip"])
@pytest.mark.parametrize("hidden", [False, True], ids=["content", "masked"])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["success", "stream", "error"])
def test_qwen_hongkong_wire(wire_tracer, encoding, compress, hidden, use_async, outcome):
    payloads = wire_tracer(encoding, compress, hidden)
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == BASE_URL + "/chat/completions"
        assert request.headers["authorization"] == "Bearer test-vendor-key"
        assert json.loads(request.content)["messages"][0]["content"] == PROMPT
        if outcome == "error":
            return unauthorized(request)
        if outcome == "stream":
            return chat_stream()
        return httpx.Response(200, json=chat_completion())

    kwargs = dict(
        model=REQUEST_MODEL,
        messages=[{"role": "user", "content": PROMPT}],
        stream=outcome == "stream",
    )

    async def run_async():
        async with async_client(handler, base_url=BASE_URL) as sdk:
            response = await sdk.chat.completions.create(**kwargs)
            if outcome == "stream":
                assert "".join([chunk.choices[0].delta.content or ""
                                async for chunk in response]) == ANSWER
            else:
                assert response.choices[0].message.content == ANSWER

    def run_sync():
        with client(handler, base_url=BASE_URL) as sdk:
            response = sdk.chat.completions.create(**kwargs)
            if outcome == "stream":
                assert "".join(chunk.choices[0].delta.content or ""
                               for chunk in response) == ANSWER
            else:
                assert response.choices[0].message.content == ANSWER

    def run():
        asyncio.run(run_async()) if use_async else run_sync()

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            run()
    else:
        run()

    assert len(seen) == len(payloads) == 1
    path, headers, payload = payloads[0]
    assert path == "/v1/traces"
    assert headers.get("Content-Encoding") == ("gzip" if compress else None)
    assert headers["Content-Type"] == (
        "application/json" if encoding == "json" else "application/x-protobuf"
    )
    spans = [span for resource in payload["resourceSpans"]
             for scope in resource["scopeSpans"] for span in scope["spans"]]
    assert len(spans) == 1
    span = spans[0]
    attrs = {a["key"]: a["value"] for a in span["attributes"]}
    assert attrs["gen_ai.provider.name"] == {"stringValue": "dashscope"}
    assert attrs["gen_ai.request.model"] == {
        "stringValue": RESPONSE_MODEL if outcome == "success" else REQUEST_MODEL
    }
    assert attrs["gen_ai.span.kind"] == {"stringValue": "LLM"}
    assert span["status"]["code"] == (2 if outcome == "error" else 1)
    if outcome == "success":
        assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "5"}
        assert attrs["gen_ai.usage.output_tokens"] == {"intValue": "2"}
    wire = json.dumps(payload)
    if hidden:
        assert PROMPT not in wire
        assert ANSWER not in wire
    else:
        assert PROMPT in wire
    assert "test-vendor-key" not in wire


def test_qwen_readme_snippet(collector, monkeypatch):
    import atexit
    from pathlib import Path

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from traceai_openai import OpenAIInstrumentor

    readme = Path(__file__).resolve().parents[3] / "examples/qwen/README.md"
    snippet = readme.read_text().split("## Code\n", 1)[1].split("```python\n", 1)[1].split("```", 1)[0]
    seen = []
    original_init = openai.OpenAI.__init__

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=chat_completion())

    def init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(openai.OpenAI, "__init__", init)
    monkeypatch.setenv("DASHSCOPE_BASE_URL", BASE_URL)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-qwen-key")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen-plus")
    namespace = {}
    try:
        exec(compile(snippet, str(readme), "exec"), namespace)
        assert len(collector) == len(seen) == 1
        assert str(seen[0].url) == BASE_URL + "/chat/completions"
        assert json.loads(seen[0].content)["model"] == "qwen-plus"
        assert seen[0].headers["authorization"] == "Bearer synthetic-qwen-key"
        assert "x-api-key" not in seen[0].headers
        assert "x-secret-key" not in seen[0].headers
        payload = ExportTraceServiceRequest.FromString(collector[0][2])
        spans = [span for resource in payload.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        assert len(spans) == 1
        attrs = {item.key: item.value for item in spans[0].attributes}
        assert attrs["gen_ai.provider.name"].string_value == "dashscope"
        assert b"synthetic-qwen-key" not in collector[0][2]
    finally:
        OpenAIInstrumentor().uninstrument()
        if "provider" in namespace:
            namespace["provider"].shutdown()
            atexit.unregister(namespace["provider"].shutdown)
