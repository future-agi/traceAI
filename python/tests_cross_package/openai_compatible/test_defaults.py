"""OpenAI-compatible provider integrations never double-trace, across the installed openai and provider packages.

Runs in the openai-compatible-providers workflow, which installs openai, httpx, traceai-openai and the five provider
packages. None of these tests may skip there."""

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import openai

PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/v1", "traceai_deepseek", "DeepSeekInstrumentor", {}),
    "fireworks": ("https://api.fireworks.ai/inference/v1", "traceai_fireworks", "FireworksInstrumentor", {}),
    "xai": ("https://api.x.ai/v1", "traceai_xai", "XAIInstrumentor", {}),
    "minimax": ("https://api.minimax.io/v1", "traceai_minimax", "MiniMaxInstrumentor", {}),
    "vllm": ("http://localhost:8000/v1", "traceai_vllm", "VLLMInstrumentor", {}),
}
_BODY = {
    "id": "c1", "object": "chat.completion", "created": 1, "model": "m",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _client(base_url):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_BODY))
    return openai.OpenAI(api_key="k", base_url=base_url, http_client=httpx.Client(transport=transport))


def _async_client(base_url):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_BODY))
    return openai.AsyncOpenAI(
        api_key="k", base_url=base_url, http_client=httpx.AsyncClient(transport=transport))


@pytest.fixture
def exporter():
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    exp.provider = provider
    return exp


def _instrument(names, provider):
    import importlib
    started = []
    for name in names:
        if name == "openai":
            module, entry = "traceai_openai", "OpenAIInstrumentor"
        else:
            _, module, entry, _ = PROVIDERS[name]
        instrumentor = getattr(importlib.import_module(module), entry)()
        instrumentor.instrument(tracer_provider=provider)
        started.append(instrumentor)
    return started


@pytest.fixture
def run(exporter):
    started = []

    def go(names):
        started.extend(_instrument(names, exporter.provider))

    yield go
    for instrumentor in reversed(started):
        instrumentor.uninstrument()


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_zero_code_defaults_one_span_per_call(name, run, exporter):
    run(["openai", name])  # both enabled, as zero-code defaults do
    _client(PROVIDERS[name][0]).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}])
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("name", sorted(PROVIDERS))
@pytest.mark.asyncio
async def test_zero_code_defaults_one_span_per_async_call(name, run, exporter):
    run(["openai", name])
    await _async_client(PROVIDERS[name][0]).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}])
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_provider_alone_still_traces_once(name, run, exporter):
    run([name])  # FI_INSTRUMENTATIONS without openai
    _client(PROVIDERS[name][0]).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}])
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_plain_openai_call_one_span(name, run, exporter):
    run(["openai", name])
    _client("https://api.openai.com/v1").chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}])
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("host,provider", [
    ("api.minimax.io", "minimax"), ("api.minimax.chat", "minimax"),
    ("api.x.ai", "xai"), ("api.deepseek.com", "deepseek"), ("api.fireworks.ai", "fireworks"),
])
def test_shared_openai_span_keeps_provider_label(host, provider):
    from traceai_openai._providers import provider_from_host
    assert provider_from_host(host) == provider
