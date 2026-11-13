"""Zero-code entry points (fi-instrument, FI_AUTO_INSTRUMENT, opentelemetry-instrument, init())
and the integration registry."""

import logging
import os
import subprocess
import sys
import textwrap
import types
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from fi_instrumentation.auto import _bootstrap, _manager, _registry
from fi_instrumentation.auto._otel_entry import FutureAGIInstrumentor

PYTHON_ROOT = Path(__file__).resolve().parents[1]
PTH_FILE = PYTHON_ROOT / "fi_instrumentation_auto.pth"


def _child_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "OTEL_"))}
    env["PYTHONPATH"] = str(PYTHON_ROOT)
    return env


def test_bootstrap_fails_open(monkeypatch, caplog):
    def explode(**_):
        raise RuntimeError("collector config broken")

    monkeypatch.setattr(_bootstrap, "_started", False)
    monkeypatch.setattr(_manager, "init", explode)
    with caplog.at_level(logging.WARNING, logger="fi_instrumentation.auto"):
        _bootstrap.start()
        _bootstrap.start()  # a second call is a no-op
    assert caplog.text.count("auto-instrumentation is off: collector config broken") == 1


def test_opentelemetry_instrument_entry_point_starts_the_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(_manager, "_state", _manager._State())
    monkeypatch.setattr(_bootstrap, "start", lambda: calls.append("start"))
    monkeypatch.setenv("FI_API_KEY", "fi-api-PLACEHOLDER")
    instrumentor = FutureAGIInstrumentor()
    assert tuple(instrumentor.instrumentation_dependencies()) == ()
    instrumentor.instrument()
    instrumentor.uninstrument()
    assert calls == ["start"]


def test_opentelemetry_entry_point_is_quiet_without_future_agi_settings(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(_manager, "_state", _manager._State())
    monkeypatch.setattr(_bootstrap, "start", lambda: calls.append("start"))
    monkeypatch.chdir(tmp_path)
    for key in ("FI_API_KEY", "FI_SECRET_KEY", "FI_CONFIG_FILE"):
        monkeypatch.delenv(key, raising=False)
    instrumentor = FutureAGIInstrumentor()
    instrumentor.instrument()
    instrumentor.uninstrument()
    assert calls == []


def _pth_line():
    lines = [line for line in PTH_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    # site.py executes only .pth lines that start with "import".
    assert lines[0].startswith("import ")
    return lines[0]


@pytest.mark.parametrize("value, starts", [
    (None, False), ("", False), ("false", False), ("0", False), ("off", False),
    ("true", True), ("1", True), (" YES ", True), ("on", True),
])
def test_pth_starts_the_agent_only_when_asked(monkeypatch, value, starts):
    calls = []
    monkeypatch.setattr(_bootstrap, "start", lambda: calls.append("start"))
    if value is None:
        monkeypatch.delenv("FI_AUTO_INSTRUMENT", raising=False)
    else:
        monkeypatch.setenv("FI_AUTO_INSTRUMENT", value)
    exec(_pth_line(), {})
    assert calls == (["start"] if starts else [])


def test_core_import_stays_light_and_the_pth_is_inert_by_default(tmp_path):
    script = tmp_path / "check.py"
    script.write_text(textwrap.dedent('''
        import sys
        exec(open(sys.argv[1]).read())
        loaded_by_pth = "fi_instrumentation" in sys.modules
        import fi_instrumentation
        print(loaded_by_pth, "fi_instrumentation.auto" in sys.modules)
    '''))
    result = subprocess.run([sys.executable, str(script), str(PTH_FILE)], env=_child_env(),
                            capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "False"]


def test_package_declares_the_zero_code_entry_points():
    text = (PYTHON_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'fi-instrument = "fi_instrumentation.auto._cli:main"' in text
    assert 'futureagi = "fi_instrumentation.auto._otel_entry:FutureAGIInstrumentor"' in text
    assert '"fi_instrumentation_auto.pth"' in text
    # The installed distribution carries the same metadata (a poetry or wheel install).
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts.get("fi-instrument") == "fi_instrumentation.auto._cli:main"
    plugins = {ep.name: ep.value for ep in entry_points(group="opentelemetry_instrumentor")}
    assert plugins.get("futureagi") == "fi_instrumentation.auto._otel_entry:FutureAGIInstrumentor"


def test_lazy_package_exports():
    import fi_instrumentation

    assert fi_instrumentation.init is _manager.init
    assert fi_instrumentation.auto_instrument is _manager.auto_instrument
    assert "init" in fi_instrumentation.__all__
    with pytest.raises(AttributeError):
        fi_instrumentation.not_a_thing  # noqa: B018


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
        if integration.kind in (_registry.MANUAL, _registry.PLUGIN, _registry.SERVICE):
            assert integration.note, integration.name
        if not integration.default:
            assert integration.note, integration.name
        # There is no HTTP-level capture in this release: no note may promise it.
        assert "wire" not in integration.note.lower(), integration.name
    assert _registry.find("Google-GenAI").name == "google_genai"
    assert _registry.find("nope") is None


@pytest.mark.parametrize("name, target, package, module, entry", [
    ("transformers", "transformers", "traceAI-transformers", "traceai_transformers", "TransformersInstrumentor"),
    ("openrouter", "openrouter", "traceAI-openrouter", "traceai_openrouter", "OpenRouterInstrumentor"),
])
def test_registry_enables_transformers_and_openrouter(name, target, package, module, entry):
    integration = _registry.find(name)
    assert integration is not None
    assert integration.kind == _registry.INSTRUMENTOR
    assert integration.targets == (target,)
    assert (integration.package, integration.module, integration.entry) == (package, module, entry)
    assert integration.default


@pytest.mark.parametrize("name, target, package", [
    ("agentscope", "agentscope", "traceAI-agentscope"),
    ("temporal", "temporalio", "traceAI-temporal"),
    ("cognee", "cognee", "traceai-cognee"),
    ("openwebui", "open_webui", "traceAI-openwebui"),
])
def test_packages_without_automatic_setup_are_reported_as_manual(name, target, package):
    integration = _registry.find(name)
    assert integration is not None
    assert integration.kind == _registry.MANUAL
    assert target in integration.targets
    assert integration.package == package
    # The report tells the user what to add.
    assert package in integration.note
