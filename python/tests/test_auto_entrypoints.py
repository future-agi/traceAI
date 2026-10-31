"""Process-start entry points and the setup adapters for non-BaseInstrumentor integrations."""

import logging
import types

import pytest

from fi_instrumentation.auto import _bootstrap, _manager, _registry
from fi_instrumentation.auto._otel_entry import FutureAGIInstrumentor


def test_bootstrap_fails_open(monkeypatch, caplog):
    def explode(**_):
        raise RuntimeError("collector config broken")

    monkeypatch.setattr(_bootstrap, "_started", False)
    monkeypatch.setattr(_manager, "init", explode)
    with caplog.at_level(logging.WARNING, logger="fi_instrumentation.auto"):
        _bootstrap.start()
        _bootstrap.start()  # second call is a no-op
    assert caplog.text.count("auto-instrumentation is off: collector config broken") == 1


def test_opentelemetry_instrument_entry_point_starts_the_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(_bootstrap, "start", lambda: calls.append("start"))
    monkeypatch.setenv("FI_API_KEY", "key")
    instrumentor = FutureAGIInstrumentor()
    assert tuple(instrumentor.instrumentation_dependencies()) == ()
    instrumentor.instrument()
    instrumentor.uninstrument()
    assert calls == ["start"]


class _Recorder:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _module(**attrs):
    return types.SimpleNamespace(**attrs)


@pytest.mark.parametrize("name", sorted(_registry.SETUPS))
def test_setup_adapters_call_the_package_entry_point(name):
    provider, config = object(), object()
    recorder = _Recorder()

    class Instrumentor:
        def instrument(self, *args, **kwargs):
            return recorder(*args, **kwargs)

        def uninstrument(self):
            pass

    recorder.result = Instrumentor()
    module = _module(
        SemanticKernelInstrumentor=Instrumentor,
        StrandsInstrumentor=Instrumentor,
        configure_agno_tracing=recorder,
        configure_beeai_tracing=recorder,
        enable_fi_attribute_mapping=recorder,
        setup=recorder,
    )
    detail = _registry.SETUPS[name](module, provider, config)
    assert detail
    assert recorder.calls


@pytest.mark.parametrize("name", ["livekit", "pipecat", "agent_framework"])
def test_mapping_adapters_report_when_nothing_was_installed(name):
    module = _module(enable_fi_attribute_mapping=_Recorder(result=False))
    with pytest.raises(RuntimeError):
        _registry.SETUPS[name](module, object(), object())


def test_registry_is_consistent():
    names = [i.name for i in _registry.ALL]
    assert len(names) == len(set(names))
    for integration in _registry.ALL:
        if integration.kind == _registry.SETUP:
            assert integration.entry in _registry.SETUPS, integration.name
        if integration.kind in (_registry.INSTRUMENTOR, _registry.SETUP):
            assert integration.entry and integration.module and integration.targets, integration.name
        if not integration.default:
            assert integration.note, integration.name
    assert _registry.find("Google-GenAI").name == "google_genai"
    assert _registry.find("nope") is None


def test_opentelemetry_entry_point_is_quiet_without_future_agi_settings(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(_bootstrap, "start", lambda: calls.append("start"))
    monkeypatch.chdir(tmp_path)
    for key in ("FI_API_KEY", "FI_SECRET_KEY", "FI_CONFIG_FILE"):
        monkeypatch.delenv(key, raising=False)
    instrumentor = FutureAGIInstrumentor()
    instrumentor.instrument()
    instrumentor.uninstrument()
    assert calls == []


def test_lazy_package_exports():
    import fi_instrumentation

    assert fi_instrumentation.init is _manager.init
    assert fi_instrumentation.auto_instrument is _manager.auto_instrument
    with pytest.raises(AttributeError):
        fi_instrumentation.not_a_thing  # noqa: B018
