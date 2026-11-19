"""OpenAI-compatible providers are on by default only when the installed provider package defers.

The default-on decision is made by the core; the deferral lives in separately released provider packages.
A provider that does not declare ``DEFERS_TO_SHARED_OPENAI`` keeps its previous state, so one span per
call holds on any mix of installed versions.
"""

import importlib
import json
import sys
import textwrap

import httpx
import pytest
from fi_instrumentation.auto import _manager, _registry
from fi_instrumentation.auto._config import AutoConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

openai = pytest.importorskip("openai")

DEFERRING = ("deepseek", "fireworks", "xai", "minimax", "vllm")
BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "xai": "https://api.x.ai/v1",
    "minimax": "https://api.minimax.io/v1",
    "vllm": "http://localhost:8000/v1",
}
PROVIDER_LABEL = {"deepseek": "deepseek", "fireworks": "fireworks", "xai": "xai", "minimax": "minimax", "vllm": None}
_BODY = {
    "id": "c1", "object": "chat.completion", "created": 1, "model": "m",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _sse():
    chunks = [
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "o"}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "k"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _handler(request):
    if json.loads(request.content or b"{}").get("stream"):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse())
    return httpx.Response(200, json=_BODY)


def _client(base_url):
    return openai.OpenAI(api_key="k", base_url=base_url, http_client=httpx.Client(transport=httpx.MockTransport(_handler)))


def _async_client(base_url):
    return openai.AsyncOpenAI(
        api_key="k", base_url=base_url, http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)))


@pytest.fixture
def exporter():
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    exp.provider = provider
    return exp


def _integration(name):
    return next(i for i in _registry.ALL if i.name == name)


def _enabled_by_default(names, monkeypatch=None):
    """The integrations the core would turn on by default among ``names``, in registry order."""
    settings = AutoConfig()
    out = []
    for name in names:
        status, action, _ = _manager._plan(_integration(name), settings, lazy=False)
        if action == "now":
            out.append(name)
    return out


def _instrument(names, provider):
    started = []
    for name in names:
        integration = _integration(name)
        instrumentor = getattr(importlib.import_module(integration.module), integration.entry)()
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


# --- the marker reader ---------------------------------------------------------------------------


def _package(tmp_path, monkeypatch, name, source):
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text(textwrap.dedent(source))
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(name, None)
    return _registry.Integration("probe", ("openai",), "probe", name, "Probe")


def test_marker_is_read_without_importing_the_package(tmp_path, monkeypatch):
    integration = _package(tmp_path, monkeypatch, "defers_probe_a", """
        DEFERS_TO_SHARED_OPENAI = True
        raise RuntimeError("the package must not be executed to read its marker")
    """)
    assert _manager.provider_defers_to_shared_openai(integration) is True
    assert "defers_probe_a" not in sys.modules


@pytest.mark.parametrize("source", ["VALUE = 1\n", "DEFERS_TO_SHARED_OPENAI = False\n", "# DEFERS_TO_SHARED_OPENAI = True\n"])
def test_missing_or_false_marker_does_not_defer(tmp_path, monkeypatch, source):
    integration = _package(tmp_path, monkeypatch, "defers_probe_b", source)
    assert _manager.provider_defers_to_shared_openai(integration) is False


def test_missing_package_does_not_defer():
    integration = _registry.Integration("probe", ("openai",), "probe", "defers_probe_not_installed", "Probe")
    assert _manager.provider_defers_to_shared_openai(integration) is False


@pytest.mark.parametrize("name", DEFERRING)
def test_installed_provider_packages_declare_the_marker(name):
    assert _manager.provider_defers_to_shared_openai(_integration(name)) is True


# --- version skew --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", DEFERRING)
def test_provider_without_the_marker_keeps_its_old_state(name, monkeypatch):
    monkeypatch.setattr(_manager, "provider_defers_to_shared_openai", lambda integration: False)
    assert name not in _enabled_by_default(["openai", name])
    status = next(s for s in _manager.plan(AutoConfig()) if s.name == name)
    assert status.status == _manager.DISABLED
    assert "already traces these calls" in status.detail


@pytest.mark.parametrize("name", DEFERRING)
def test_provider_with_the_marker_is_on_by_default(name, monkeypatch):
    monkeypatch.setattr(_manager, "provider_defers_to_shared_openai", lambda integration: True)
    assert name in _enabled_by_default(["openai", name])


@pytest.mark.parametrize("name", DEFERRING)
def test_listing_a_provider_explicitly_still_selects_it(name, monkeypatch):
    monkeypatch.setattr(_manager, "provider_defers_to_shared_openai", lambda integration: False)
    settings = AutoConfig(instrumentations=frozenset({name}))
    _, action, _ = _manager._plan(_integration(name), settings, lazy=False)
    assert action == "now"


def _old_non_deferring(name, monkeypatch):
    """A provider package released before the deferral: it always wraps its own calls."""
    module = importlib.import_module(_integration(name).module)
    owners = [module]
    for submodule in ("_wrappers", "_utils"):
        try:
            owners.append(importlib.import_module(f"{module.__name__}.{submodule}"))
        except ImportError:
            pass
    for owner in owners:
        for attribute in ("_shared_instrumentor_enabled", "shared_openai_is_active"):
            if hasattr(owner, attribute):
                monkeypatch.setattr(owner, attribute, lambda: False)


@pytest.mark.parametrize("name", DEFERRING)
def test_old_provider_without_the_marker_gives_one_span(name, monkeypatch, run, exporter):
    _old_non_deferring(name, monkeypatch)
    monkeypatch.setattr(_manager, "provider_defers_to_shared_openai", lambda integration: False)
    run(_enabled_by_default(["openai", name]))
    _client(BASE_URLS[name]).chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("name", DEFERRING)
def test_provider_with_the_marker_gives_one_span_with_its_label(name, run, exporter):
    run(_enabled_by_default(["openai", name]))
    _client(BASE_URLS[name]).chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    (span,) = exporter.get_finished_spans()
    if PROVIDER_LABEL[name]:
        assert span.attributes["gen_ai.provider.name"] == PROVIDER_LABEL[name]
    assert span.attributes["gen_ai.request.model"] == "m"


def test_deepseek_needs_the_shared_extensions(monkeypatch):
    openai_module = importlib.import_module("traceai_openai")
    monkeypatch.setattr(openai_module.OpenAIInstrumentor, "_supports_deepseek_extensions", False)
    assert "deepseek" not in _enabled_by_default(["openai", "deepseek"])
    assert "xai" in _enabled_by_default(["openai", "xai"])


# --- streaming -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["xai", "minimax"])
def test_streaming_gives_one_span(name, run, exporter):
    run(_enabled_by_default(["openai", name]))
    stream = _client(BASE_URLS[name]).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True)
    assert "".join(c.choices[0].delta.content or "" for c in stream if c.choices) == "ok"
    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.provider.name"] == name


@pytest.mark.parametrize("name", ["xai", "minimax"])
@pytest.mark.asyncio
async def test_async_streaming_gives_one_span(name, run, exporter):
    run(_enabled_by_default(["openai", name]))
    stream = await _async_client(BASE_URLS[name]).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True)
    text = ""
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
    assert text == "ok"
    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.provider.name"] == name


# --- teardown order ------------------------------------------------------------------------------


def test_cleanups_run_in_reverse_order():
    calls = []
    _manager._run_cleanups({"a": lambda: calls.append("a"), "b": lambda: calls.append("b"), "c": lambda: calls.append("c")})
    assert calls == ["c", "b", "a"]


def test_shutdown_order_leaves_no_wrapper_and_a_later_instrument_gives_one_span(exporter):
    chat = importlib.import_module("openai.resources.chat")
    targets = [(openai.OpenAI, "request"), (openai.AsyncOpenAI, "request"), (chat.Completions, "create"), (chat.AsyncCompletions, "create")]
    originals = [getattr(owner, attribute) for owner, attribute in targets]
    names = ["openai", *DEFERRING]
    cleanups = {}
    for instrumentor, name in zip(_instrument(names, exporter.provider), names):
        cleanups[name] = instrumentor.uninstrument
    _manager._run_cleanups(cleanups)
    for (owner, attribute), original in zip(targets, originals):
        assert getattr(owner, attribute) is original, f"{owner.__name__}.{attribute} still wrapped"
    started = _instrument(["openai", "xai"], exporter.provider)
    try:
        _client(BASE_URLS["xai"]).chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
        assert len(exporter.get_finished_spans()) == 1
    finally:
        for instrumentor in reversed(started):
            instrumentor.uninstrument()
