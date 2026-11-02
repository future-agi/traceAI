"""Baseten attribution through the real OpenAI SDK and a loopback OTLP receiver.

HTTP model responses and credentials are synthetic. Resource coverage asserts
instrumentor behavior, not that every Baseten deployment serves every API.
"""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType

from traceai_openai import OpenAIInstrumentor
from traceai_openai._providers import provider_from_host

from .test_openai_compatible_spans import (
    ANSWER, REQUEST_MODEL, RESPONSE_MODEL, async_client, chat_completion,
    chat_stream, client, embeddings_body, responses_body, responses_stream, unauthorized,
)

# Use the repository's collector harness instead of a second telemetry implementation.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from harness import Receiver, _flatten_attributes

BASE_URLS = [
    "https://inference.baseten.co/v1",
    "https://model-abc123.api.baseten.co/environments/production/sync/v1",
    "https://model-abc123-us-west.api.baseten.co/sync/v1",
]
PROMPT = "synthetic-baseten-private-prompt"


@pytest.mark.parametrize("host,expected", [
    ("inference.baseten.co", "baseten"),
    ("model-abc123.api.baseten.co", "baseten"),
    ("model-abc123-us-west.api.baseten.co", "baseten"),
    ("MODEL-Abc123.API.BASETEN.CO.", "baseten"),
    ("model-a.api.baseten.co", "baseten"),
    ("model-" + "a" * 57 + ".api.baseten.co", "baseten"),
    ("model-" + "a" * 58 + ".api.baseten.co", "openai"),
    ("model-.api.baseten.co", "openai"),
    ("model--abc.api.baseten.co", "openai"),
    ("model-abc-.api.baseten.co", "openai"),
    ("model-abc_123.api.baseten.co", "openai"),
    ("model-abc.123.api.baseten.co", "openai"),
    ("model-abc.api.baseten.co.customer.example", "openai"),
    ("proxy.model-abc.api.baseten.co", "openai"),
    ("notmodel-abc.api.baseten.co", "openai"),
    ("chain-abc.api.baseten.co", "openai"),
    ("api.baseten.co", "openai"),
    ("baseten.co", "openai"),
    ("inference.baseten.co.customer.example", "openai"),
    ("api.openai.com", "openai"),
    (None, "openai"),
])
def test_baseten_host_boundaries(host, expected):
    assert provider_from_host(host) == expected


@pytest.fixture(scope="module")
def receiver():
    with Receiver() as sink:
        yield sink


@pytest.fixture
def wire_tracer(receiver, monkeypatch):
    # No inherited telemetry destinations, auth, privacy settings or HTTP proxies.
    import os
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_", "OPENAI_")) or name.lower() in (
            "http_proxy", "https_proxy", "all_proxy", "no_proxy"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("FI_API_KEY", "synthetic-fi-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-fi-secret")
    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    receiver.clear()
    provider = register(
        project_name="baseten-contract", project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=False, verbose=False,
    )
    instrumentor = OpenAIInstrumentor()

    def configure(hidden=False):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(
            hide_inputs=hidden, hide_outputs=hidden,
            hide_llm_invocation_parameters=hidden,
        ))
        return provider

    try:
        yield configure
    finally:
        instrumentor.uninstrument()
        provider.force_flush()
        provider.shutdown()


def only_span(receiver, provider):
    assert provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    exports = receiver.requests()
    assert len(exports) == 1
    assert exports[0]["path"] == "/tracer/v1/traces"
    assert exports[0]["headers"]["x-api-key"] == "synthetic-fi-key"
    assert exports[0]["headers"]["x-secret-key"] == "synthetic-fi-secret"
    assert exports[0]["resource_attributes"][0]["project_name"] == "baseten-contract"
    assert exports[0]["resource_attributes"][0]["project_type"] == "observe"
    for secret in ("test-vendor-key", "synthetic-fi-key", "synthetic-fi-secret"):
        assert secret not in json.dumps(spans)
    return spans[0], _flatten_attributes(spans[0]["attributes"])


@pytest.mark.parametrize("base_url", BASE_URLS, ids=["model-api", "dedicated", "regional"])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("resource,outcome", [
    ("chat", "success"), ("chat", "stream"), ("chat", "error"),
    ("responses", "success"), ("responses", "stream"), ("responses", "error"),
    ("embeddings", "success"), ("embeddings", "error"),
])
@pytest.mark.parametrize("hidden", [False, True], ids=["content", "masked"])
def test_baseten_wire_contract(receiver, wire_tracer, base_url, use_async, resource, outcome, hidden):
    provider = wire_tracer(hidden)
    seen = []
    route = "chat/completions" if resource == "chat" else resource

    def handler(request):
        seen.append(request)
        assert request.url.path == urlsplit(base_url).path + "/" + route
        assert request.headers["authorization"] == "Bearer test-vendor-key"
        if outcome == "error":
            return unauthorized(request)
        if outcome == "stream":
            return chat_stream() if resource == "chat" else responses_stream()
        body = {"chat": chat_completion, "responses": responses_body,
                "embeddings": embeddings_body}[resource]()
        return httpx.Response(200, json=body)

    kwargs = {"model": REQUEST_MODEL}
    if resource == "chat":
        kwargs.update(messages=[{"role": "user", "content": PROMPT}], stream=outcome == "stream")
    else:
        kwargs["input"] = PROMPT
        if resource == "responses":
            kwargs["stream"] = outcome == "stream"
        else:
            kwargs["encoding_format"] = "float"

    def create(sdk):
        endpoint = sdk.chat.completions if resource == "chat" else getattr(sdk, resource)
        return endpoint.create(**kwargs)

    async def run_async():
        async with async_client(handler, base_url=base_url) as sdk:
            response = await create(sdk)
            if outcome == "stream":
                async for _ in response:
                    pass

    def run_sync():
        with client(handler, base_url=base_url) as sdk:
            response = create(sdk)
            if outcome == "stream":
                list(response)

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            asyncio.run(run_async()) if use_async else run_sync()
    else:
        asyncio.run(run_async()) if use_async else run_sync()
    assert len(seen) == 1
    span, attrs = only_span(receiver, provider)
    assert attrs["gen_ai.provider.name"] == "baseten"
    assert attrs["gen_ai.request.model"] == (
        REQUEST_MODEL if outcome == "error" or (resource == "chat" and outcome == "stream")
        else RESPONSE_MODEL
    )
    assert span["status"]["code"] == ("STATUS_CODE_ERROR" if outcome == "error" else "STATUS_CODE_OK")
    if resource == "chat" and outcome == "success":
        assert int(attrs["gen_ai.usage.input_tokens"]) == 5
        assert int(attrs["gen_ai.usage.output_tokens"]) == 2
    if hidden:
        assert PROMPT not in json.dumps(span)
        assert ANSWER not in json.dumps(span)


@pytest.mark.parametrize("base_url,expected", [
    ("https://MODEL-Abc123.API.BASETEN.CO.:443/v1", "baseten"),
    ("https://model-abc.api.baseten.co.customer.example/v1", "openai"),
    ("https://proxy.model-abc.api.baseten.co/v1", "openai"),
    ("https://model-abc.api.baseten.co@customer.example/v1", "openai"),
    ("https://customer.example/model-abc.api.baseten.co/v1", "openai"),
])
def test_baseten_url_controls(receiver, wire_tracer, base_url, expected):
    provider = wire_tracer()
    with client(lambda _: httpx.Response(200, json=chat_completion()), base_url=base_url) as sdk:
        sdk.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": PROMPT}])
    _, attrs = only_span(receiver, provider)
    assert attrs["gen_ai.provider.name"] == expected


def test_baseten_client_endpoint_changes(receiver, wire_tracer):
    provider = wire_tracer()
    with client(lambda _: httpx.Response(200, json=chat_completion())) as sdk:
        for base_url, expected in [
            *[(url, "baseten") for url in BASE_URLS],
            ("https://customer.example/v1", "openai"),
        ]:
            sdk.base_url = base_url
            sdk.chat.completions.create(
                model=REQUEST_MODEL, messages=[{"role": "user", "content": PROMPT}],
            )
            _, attrs = only_span(receiver, provider)
            assert attrs["gen_ai.provider.name"] == expected
            receiver.clear()
