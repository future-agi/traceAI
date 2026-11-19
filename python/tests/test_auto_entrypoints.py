"""Zero-code entry points (fi-instrument, FI_AUTO_INSTRUMENT, opentelemetry-instrument, init())
and the integration registry."""

import dataclasses
import logging
import os
import subprocess
import sys
import textwrap
import types
from importlib.metadata import entry_points
from pathlib import Path
from typing import Optional

import pytest

from fi_instrumentation.auto import _bootstrap, _manager, _registry
from fi_instrumentation.auto._otel_entry import FutureAGIInstrumentor

PYTHON_ROOT = Path(__file__).resolve().parents[1]
PTH_FILE = PYTHON_ROOT / "fi_instrumentation_auto.pth"


def _child_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "OTEL_"))}
    env["PYTHONPATH"] = str(PYTHON_ROOT)
    return env


def _run_child(tmp_path, source, env=None):
    script = tmp_path / "check.py"
    script.write_text(textwrap.dedent(source))
    result = subprocess.run([sys.executable, str(script)], env=env or _child_env(),
                            capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    return result


def test_bootstrap_fails_open(monkeypatch, caplog):
    def explode(**_):
        raise RuntimeError("collector config broken")

    monkeypatch.setattr(_bootstrap, "_started", False)
    monkeypatch.setattr(_manager, "init", explode)
    with caplog.at_level(logging.WARNING, logger="fi_instrumentation.auto"):
        _bootstrap.start()
        _bootstrap.start()  # a second call is a no-op
    assert caplog.text.count("auto-instrumentation is off: collector config broken") == 1


@pytest.mark.parametrize("opt_in, starts", [
    (None, False), ("", False), ("false", False), ("0", False),
    ("true", True), ("1", True), (" YES ", True), ("on", True),
])
def test_opentelemetry_instrument_entry_point_needs_the_explicit_opt_in(monkeypatch, tmp_path, opt_in, starts):
    calls = []
    monkeypatch.setattr(_manager, "_state", _manager._State())
    monkeypatch.setattr(_bootstrap, "start", lambda: calls.append("start"))
    monkeypatch.setattr(_bootstrap, "_started", False)
    monkeypatch.setattr(_manager, "shutdown", lambda *args, **kwargs: calls.append("shutdown"))
    # Keys and a futureagi.toml are configuration for a manual setup, not a request to start the agent.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "futureagi.toml").write_text('project_name = "manual-setup"\n')
    monkeypatch.setenv("FI_API_KEY", "fi-api-PLACEHOLDER")
    monkeypatch.setenv("FI_SECRET_KEY", "fi-secret-PLACEHOLDER")
    if opt_in is None:
        monkeypatch.delenv("FI_AUTO_INSTRUMENT", raising=False)
    else:
        monkeypatch.setenv("FI_AUTO_INSTRUMENT", opt_in)
    instrumentor = FutureAGIInstrumentor()
    assert tuple(instrumentor.instrumentation_dependencies()) == ()
    instrumentor.instrument()
    instrumentor.uninstrument()
    # uninstrument() stops only an agent that started.
    assert calls == (["start"] if starts else [])


def test_opentelemetry_entry_point_is_quiet_without_future_agi_settings(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(_manager, "_state", _manager._State())
    monkeypatch.setattr(_bootstrap, "start", lambda: calls.append("start"))
    monkeypatch.chdir(tmp_path)
    for key in ("FI_API_KEY", "FI_SECRET_KEY", "FI_CONFIG_FILE", "FI_AUTO_INSTRUMENT"):
        monkeypatch.delenv(key, raising=False)
    instrumentor = FutureAGIInstrumentor()
    instrumentor.instrument()
    instrumentor.uninstrument()
    assert calls == []


def test_loading_the_entry_point_does_not_load_the_agent(tmp_path):
    # opentelemetry-instrument loads every installed entry point at each start.
    env = _child_env()
    env.update({"FI_API_KEY": "fi-api-PLACEHOLDER", "FI_SECRET_KEY": "fi-secret-PLACEHOLDER"})
    result = _run_child(tmp_path, '''
        import sys
        from fi_instrumentation.auto._otel_entry import FutureAGIInstrumentor

        instrumentor = FutureAGIInstrumentor()
        instrumentor.instrument()
        instrumentor.uninstrument()
        print("fi_instrumentation.auto._manager" in sys.modules)
    ''', env)
    assert result.stdout.split() == ["False"]


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


_WARNED = "FI_AUTO_INSTRUMENT_WARNED"


def test_pth_prints_one_line_when_the_package_cannot_be_imported(monkeypatch, capsys):
    monkeypatch.setenv("FI_AUTO_INSTRUMENT", "true")
    # The marker that keeps child processes quiet starts unset and is removed after the test.
    monkeypatch.setenv(_WARNED, "")
    monkeypatch.setitem(sys.modules, "fi_instrumentation.auto._bootstrap", None)
    exec(_pth_line(), {})
    exec(_pth_line(), {})
    err = capsys.readouterr().err
    assert err.count("traceAI: ") == 1
    assert "cannot import fi_instrumentation" in err


def test_pth_leaves_the_start_to_opentelemetry_instrument(monkeypatch):
    # opentelemetry-instrument puts its sitecustomize directory on PYTHONPATH. That sitecustomize
    # configures the application's provider and then loads the futureagi entry point, which starts
    # the agent on that provider; starting earlier from the .pth would take the global provider first.
    calls = []
    monkeypatch.setattr(_bootstrap, "start", lambda: calls.append("start"))
    monkeypatch.setenv("FI_AUTO_INSTRUMENT", "true")
    otel_start = os.path.join("site-packages", "opentelemetry", "instrumentation", "auto_instrumentation")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([otel_start, "app"]))
    exec(_pth_line(), {})
    assert calls == []


def test_fi_instrument_says_once_when_the_program_cannot_load_the_agent(tmp_path):
    # fi-instrument from one environment launching a Python from another, without the package.
    elsewhere = tmp_path / "elsewhere" / "fi_instrumentation"
    elsewhere.mkdir(parents=True)
    (elsewhere / "__init__.py").write_text('raise ImportError("not installed for this Python")\n')
    env = _child_env()
    env.update({"FI_AUTO_INSTRUMENT": "true",
                "PYTHONPATH": os.pathsep.join([str(_cli_sitecustomize_dir()), str(elsewhere.parent)])})
    # The program starts a child Python, which inherits the environment.
    result = _run_child(tmp_path, '''
        import subprocess, sys
        sys.exit(subprocess.run([sys.executable, "-c", "pass"]).returncode)
    ''', env)
    assert result.stderr.count("traceAI: ") == 1, result.stderr
    assert "cannot import fi_instrumentation" in result.stderr


def _cli_sitecustomize_dir():
    return PYTHON_ROOT / "fi_instrumentation" / "auto" / "_sitecustomize"


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
    # Not star-exported, so `from fi_instrumentation import *` neither loads the agent nor binds init.
    assert "init" not in fi_instrumentation.__all__
    assert "auto_instrument" not in fi_instrumentation.__all__
    with pytest.raises(AttributeError):
        fi_instrumentation.not_a_thing  # noqa: B018


def test_star_import_does_not_load_the_agent(tmp_path):
    result = _run_child(tmp_path, '''
        import sys
        from fi_instrumentation import *  # noqa: F403

        print("fi_instrumentation.auto" in sys.modules, "init" in dir())
    ''')
    assert result.stdout.split() == ["False", "False"]


class _Recorder:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _module(**attrs):
    return types.SimpleNamespace(**attrs)


def _stand_in_openinference(monkeypatch):
    """The OpenInference TraceConfig one setup function takes instead of fi_instrumentation's."""

    @dataclasses.dataclass(frozen=True)
    class TraceConfig:
        hide_inputs: Optional[bool] = None
        hide_outputs: Optional[bool] = None
        hide_input_images: Optional[bool] = None
        base64_image_max_length: Optional[int] = None

    package = types.ModuleType("openinference")
    instrumentation = types.ModuleType("openinference.instrumentation")
    instrumentation.TraceConfig = TraceConfig
    package.instrumentation = instrumentation
    monkeypatch.setitem(sys.modules, "openinference", package)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", instrumentation)
    return TraceConfig


def _setup_module(recorder):
    class Instrumentor:
        def instrument(self, *args, **kwargs):
            return recorder(*args, **kwargs)

        def uninstrument(self):
            pass

    recorder.result = Instrumentor()
    return _module(
        SemanticKernelInstrumentor=Instrumentor,
        StrandsInstrumentor=Instrumentor,
        configure_agno_tracing=recorder,
        configure_beeai_tracing=recorder,
        enable_fi_attribute_mapping=recorder,
        setup=recorder,
    )


@pytest.mark.parametrize("name", sorted(_registry.SETUPS))
def test_setup_adapters_call_the_package_entry_point(name, monkeypatch):
    from fi_instrumentation import TraceConfig

    _stand_in_openinference(monkeypatch)
    recorder = _Recorder()
    detail = _registry.SETUPS[name](_setup_module(recorder), object(), TraceConfig())
    assert detail
    assert recorder.calls


# Setup adapters whose package takes a TraceConfig. The voice integration takes it on the Future AGI
# route instead (test_voice_route_rechecks_recordings_with_the_agent_config); the others take none.
_CONFIG_ADAPTERS = ("ag2_classic", "agent_framework", "agno", "semantic_kernel")
_NO_CONFIG_ADAPTERS = ("beeai", "livekit", "pipecat")


def test_every_setup_adapter_is_checked_for_its_trace_config():
    assert sorted(_registry.SETUPS) == sorted(_CONFIG_ADAPTERS + _NO_CONFIG_ADAPTERS)


@pytest.mark.parametrize("name", _CONFIG_ADAPTERS)
def test_setup_adapters_pass_the_trace_config(name, monkeypatch):
    from fi_instrumentation import TraceConfig

    openinference_config = _stand_in_openinference(monkeypatch)
    config = TraceConfig(hide_inputs=True, base64_image_max_length=64)
    recorder = _Recorder()
    _registry.SETUPS[name](_setup_module(recorder), object(), config)
    ((_, kwargs),) = recorder.calls
    passed = kwargs.get("config")
    if name == "agno":
        # That package takes OpenInference's TraceConfig: the agent's hide settings, converted.
        assert isinstance(passed, openinference_config)
        assert passed.hide_inputs is True
        assert passed.base64_image_max_length == 64
        # A setting that hides nothing is left unset, so the package's own variables still apply.
        assert passed.hide_outputs is None
    else:
        assert passed is config


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
    ("marqo", "marqo", "traceAI-marqo", "traceai_marqo", "MarqoInstrumentor"),
    ("watsonx", "ibm_watsonx_ai", "traceAI-watsonx", "traceai_watsonx", "WatsonxInstrumentor"),
])
def test_registry_enables_transformers_and_openrouter(name, target, package, module, entry):
    integration = _registry.find(name)
    assert integration is not None
    assert integration.kind == _registry.INSTRUMENTOR
    assert integration.targets == (target,)
    assert (integration.package, integration.module, integration.entry) == (package, module, entry)
    assert integration.default


def test_registry_enables_sagemaker_on_boto3_next_to_bedrock():
    sagemaker = _registry.find("sagemaker")
    bedrock = _registry.find("bedrock")
    assert sagemaker is not None
    assert sagemaker.kind == _registry.INSTRUMENTOR
    assert sagemaker.targets == ("boto3",)
    assert (sagemaker.package, sagemaker.module, sagemaker.entry) == (
        "traceAI-sagemaker", "traceai_sagemaker", "SageMakerInstrumentor")
    assert sagemaker.default
    # Both hook botocore client creation, so both must be enabled by the same module.
    assert bedrock.targets == sagemaker.targets


def test_registry_enables_alephalpha_on_the_aleph_alpha_client_import():
    integration = _registry.find("alephalpha")
    assert integration is not None
    assert integration.kind == _registry.INSTRUMENTOR
    assert integration.targets == ("aleph_alpha_client",)
    assert (integration.package, integration.module, integration.entry) == (
        "traceAI-alephalpha", "traceai_alephalpha", "AlephAlphaInstrumentor")
    assert integration.default


def test_registry_enables_writer_on_the_writerai_import():
    integration = _registry.find("writer")
    assert integration is not None
    assert integration.kind == _registry.INSTRUMENTOR
    assert integration.targets == ("writerai",)
    assert (integration.package, integration.module, integration.entry) == (
        "traceAI-writer", "traceai_writer", "WriterInstrumentor")
    assert integration.default


@pytest.mark.parametrize("name, target, package", [
    ("agentscope", "agentscope", "traceAI-agentscope"),
    ("temporal", "temporalio", "traceAI-temporal"),
    ("cognee", "cognee", "traceai-cognee"),
    ("openwebui", "open_webui", "traceAI-openwebui"),
    ("strands", "strands", "traceai-strands"),
])
def test_packages_without_automatic_setup_are_reported_as_manual(name, target, package):
    integration = _registry.find(name)
    assert integration is not None
    assert integration.kind == _registry.MANUAL
    assert target in integration.targets
    assert integration.package == package
    # The report tells the user what to add.
    assert package in integration.note


def test_an_integration_without_an_export_adapter_gives_its_setup_line():
    # Its package converts spans and applies privacy settings in an exporter wrapper that the
    # agent does not install, so it is reported as manual with the line from its README.
    integration = _registry.find("strands")
    assert "StrandsSpanExporter" in integration.note
    assert "strands" not in _registry.SETUPS


def test_registry_enables_agentspec_on_the_pyagentspec_import():
    integration = _registry.find("agentspec")
    assert integration is not None
    assert integration.kind == _registry.INSTRUMENTOR
    assert integration.targets == ("pyagentspec",)
    assert (integration.package, integration.module, integration.entry) == (
        "traceAI-agentspec", "traceai_agentspec", "AgentSpecInstrumentor")
    assert integration.default


def test_registry_enables_google_generativeai_on_the_legacy_sdk_import():
    integration = _registry.find("google_generativeai")
    assert integration is not None
    assert integration.kind == _registry.INSTRUMENTOR
    assert integration.targets == ("google.generativeai",)
    assert (integration.package, integration.module, integration.entry) == (
        "traceAI-google-generativeai", "traceai_google_generativeai", "GoogleGenerativeAIInstrumentor")
    assert integration.default
    # The new SDK keeps its own integration.
    assert _registry.find("google_genai").targets == ("google.genai",)
