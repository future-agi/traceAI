"""Run the README's actual Quickstart and gateway blocks without vendor traffic."""

import json
import re
from pathlib import Path

import openai
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from tests.test_vercel_gateway import (
    ANSWER,
    GATEWAY_KEY,
    REQUEST_MODEL,
    attributes,
    gateway_response,
    sdk_httpx,
)

pytest_plugins = ("tests.test_vercel_gateway",)


@pytest.mark.parametrize(
    "section,host,provider,model",
    [
        ("Quickstart", "api.openai.com", "openai", "gpt-4o-mini"),
        ("Vercel AI Gateway", "ai-gateway.vercel.sh", "vercel", REQUEST_MODEL),
    ],
)
def test_readme_request_blocks(collector, monkeypatch, section, host, provider, model):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    quickstart = readme.split("## Quickstart\n", 1)[1].split(
        "## Vercel AI Gateway\n", 1
    )[0]
    setup, instrument, openai_request = re.findall(
        r"```python\n(.*?)```", quickstart, flags=re.DOTALL
    )
    gateway = readme.split("## Vercel AI Gateway\n", 1)[1]
    gateway_request = re.findall(r"```python\n(.*?)```", gateway, flags=re.DOTALL)
    assert len(gateway_request) == 1
    request = gateway_request[0] if section == "Vercel AI Gateway" else openai_request

    seen = []
    original_init = openai.OpenAI.__init__

    def handle(http_request):
        seen.append(http_request)
        return gateway_response("success", True)

    def mock_transport_only(self, *args, **kwargs):
        kwargs["http_client"] = sdk_httpx.Client(
            transport=sdk_httpx.MockTransport(handle), trust_env=False
        )
        original_init(self, *args, **kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", GATEWAY_KEY)
    monkeypatch.setenv("AI_GATEWAY_MODEL", REQUEST_MODEL)
    monkeypatch.setattr(openai.OpenAI, "__init__", mock_transport_only)
    namespace = {}
    try:
        for block in (setup, instrument, request):
            # The README is repository-owned; executing it catches stale SDK APIs.
            exec(  # noqa: S102
                compile(
                    block,
                    str(Path(__file__).resolve().parents[1] / "README.md"),
                    "exec",
                ),
                namespace,
            )
        assert len(collector) == 1  # The README flushes its own short-lived trace.
    finally:
        if "trace_provider" in namespace:
            namespace["trace_provider"].shutdown()
        from traceai_openai import OpenAIInstrumentor

        OpenAIInstrumentor().uninstrument()

    assert len(seen) == len(collector) == 1
    assert seen[0].url.host == host
    assert json.loads(seen[0].content)["model"] == model
    if section == "Vercel AI Gateway":
        assert seen[0].headers["authorization"] == f"Bearer {GATEWAY_KEY}"
    wire = ExportTraceServiceRequest.FromString(collector[0][2])
    spans = [
        span
        for resource in wire.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    assert len(spans) == 1
    assert attributes(spans[0].attributes)["gen_ai.provider.name"] == provider
    assert ANSWER.encode() in collector[0][2]
