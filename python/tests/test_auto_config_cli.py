"""futureagi.toml, precedence, the fi-instrument command, and real zero-code runs of an unmodified app.

The end-to-end runs use a stand-in ``openai`` module and ``traceai_openai`` instrumentor written to a
temporary directory, so they need no network installs. The same runs against the real packages
and the installed wheel are in test_auto_e2e_wheel.py.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from auto_mock_server import MockServer, decode_spans
from fi_instrumentation.auto import _cli, _manager
from fi_instrumentation.auto._config import ConfigError, parse_config_text, resolve

PYTHON_ROOT = Path(__file__).resolve().parents[1]
NOT_SET = "FI_API_KEY and FI_SECRET_KEY are not set"

STAND_IN_LIBRARY = '''
import json
import urllib.request


def complete(base_url, prompt):
    body = json.dumps({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}]}).encode()
    request = urllib.request.Request(base_url + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())["choices"][0]["message"]["content"]
'''

STAND_IN_INSTRUMENTOR = '''
import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from fi_instrumentation import FITracer, TraceConfig


class OpenAIInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return ()

    def _instrument(self, **kwargs):
        tracer = FITracer(trace_api.get_tracer(__name__, None, kwargs.get("tracer_provider")),
                          config=kwargs.get("config") or TraceConfig())

        def traced(wrapped, instance, args, call_kwargs):
            attributes = {"gen_ai.span.kind": "LLM", "input.value": args[1]}
            with tracer.start_as_current_span("ChatCompletion", attributes=attributes) as span:
                result = wrapped(*args, **call_kwargs)
                span.set_attribute("output.value", result)
                return result

        wrapt.wrap_function_wrapper("openai", "complete", traced)

    def _uninstrument(self, **kwargs):
        pass
'''

APP = textwrap.dedent('''
    import json, sys
    import openai

    replies = [openai.complete(sys.argv[1], "ping"), openai.complete(sys.argv[1], "ping again")]
    wire = any(name.startswith("fi_instrumentation.auto.wire") for name in sys.modules)
    print(json.dumps({"replies": replies, "wire_loaded": wire}))
''')

SLEEPER = textwrap.dedent('''
    import sys, time
    import openai

    openai.complete(sys.argv[1], "ping")
    print("ready", flush=True)
    time.sleep(60)
''')


def _stand_ins(root: Path, anthropic: bool = False) -> Path:
    packages = {"openai": STAND_IN_LIBRARY, "traceai_openai": STAND_IN_INSTRUMENTOR}
    if anthropic:
        packages["anthropic"] = ""
    for name, source in packages.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "__init__.py").write_text(source)
    return root


def test_flat_toml_subset():
    values = parse_config_text(textwrap.dedent('''
        # comment
        project_name = "checkout # agent"   # trailing comment
        instrumentations = ["openai", "langchain"]
        debug = false
        sample_rate = 0.25
        base64_image_max_length = 1024
        [ignored.table]
        project_name = "not me"
    '''))
    assert values == {
        "project_name": "checkout # agent",
        "instrumentations": ["openai", "langchain"],
        "debug": False,
        "sample_rate": 0.25,
        "base64_image_max_length": 1024,
    }
    with pytest.raises(ConfigError):
        parse_config_text("not a line")
    with pytest.raises(ConfigError):
        parse_config_text("instrumentations = [1, 2]")


def test_unreadable_values_are_never_echoed():
    with pytest.raises(ConfigError) as error:
        parse_config_text("api_key = fi-api-PLACEHOLDER")
    assert "PLACEHOLDER" not in str(error.value)


def test_precedence_argument_env_file_default(tmp_path):
    config = tmp_path / "futureagi.toml"
    config.write_text('project_name = "from-file"\nsample_rate = 0.5\ndebug = true\nproject_type = "experiment"\n')
    environ = {"FI_PROJECT_NAME": "from-env", "FI_API_KEY": "fi-api-PLACEHOLDER",
               "FI_SECRET_KEY": "fi-secret-PLACEHOLDER"}
    settings = resolve({"project_type": "observe"}, environ=environ, config_file=str(config))
    assert settings.project_name == "from-env"
    assert settings.sample_rate == 0.5
    assert settings.debug is True
    assert settings.project_type == "observe"
    masked = settings.masked()
    assert masked["api_key"]["value"] == "fi-a…"
    assert "PLACEHOLDER" not in json.dumps(masked, default=str)
    assert masked["project_name"]["source"] == "env FI_PROJECT_NAME"
    assert masked["sample_rate"]["source"].startswith("file ")
    assert masked["project_type"]["source"] == "argument"
    with pytest.raises(ConfigError):
        resolve(environ={"FI_PROJECT_TYPE": "prod"}, config_file=str(config))
    with pytest.raises(ConfigError):
        resolve(environ={}, config_file=str(tmp_path / "missing.toml"))


def test_credentials_are_one_pair_per_source(tmp_path, caplog):
    config = tmp_path / "futureagi.toml"
    config.write_text('api_key = "file-api-PLACEHOLDER"\nsecret_key = "file-secret-PLACEHOLDER"\n')
    with caplog.at_level(logging.WARNING, logger="fi_instrumentation.auto"):
        settings = resolve(environ={"FI_API_KEY": "env-api-PLACEHOLDER"}, config_file=str(config))
    # An environment key alone never pairs with the file's secret: the whole file pair wins.
    assert (settings.api_key, settings.secret_key) == ("file-api-PLACEHOLDER", "file-secret-PLACEHOLDER")
    assert "missing FI_SECRET_KEY" in caplog.text
    assert "PLACEHOLDER" not in caplog.text
    settings = resolve({"api_key": "arg-api-PLACEHOLDER", "secret_key": "arg-secret-PLACEHOLDER"},
                       environ={"FI_API_KEY": "env-api-PLACEHOLDER", "FI_SECRET_KEY": "env-secret-PLACEHOLDER"})
    assert settings.api_key == "arg-api-PLACEHOLDER"
    assert settings.sources["api_key"] == "argument"


def test_privacy_settings_come_from_the_config_file(tmp_path, monkeypatch):
    for key in ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUT_TEXT", "FI_BASE64_IMAGE_MAX_LENGTH"):
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "futureagi.toml"
    config.write_text("hide_inputs = true\nhide_output_text = true\nbase64_image_max_length = 64\n")
    settings = resolve(environ={}, config_file=str(config))
    assert settings.trace_config.hide_inputs is True
    assert settings.trace_config.hide_output_text is True
    assert settings.trace_config.base64_image_max_length == 64
    assert settings.masked()["hide_inputs"]["source"].startswith("file ")
    # The environment still wins over the file.
    monkeypatch.setenv("FI_HIDE_INPUTS", "false")
    settings = resolve(environ={"FI_HIDE_INPUTS": "false"}, config_file=str(config))
    assert settings.trace_config.hide_inputs is False
    assert settings.masked()["hide_inputs"]["source"] == "env FI_HIDE_INPUTS"
    config.write_text("hide_outputs = 3\n")
    with pytest.raises(ConfigError, match="hide_outputs"):
        resolve(environ={}, config_file=str(config))


def test_invalid_lower_precedence_settings_do_not_block_override(tmp_path):
    config = tmp_path / "futureagi.toml"
    config.write_text("sample_rate = invalid\nproject_type = invalid\n")
    settings = resolve({"sample_rate": 0.5, "project_type": "observe"},
                       environ={"FI_SAMPLE_RATE": "invalid", "FI_PROJECT_TYPE": "invalid"}, config_file=str(config))
    assert settings.sample_rate == 0.5
    assert settings.project_type == "observe"
    settings = resolve(environ={"FI_SAMPLE_RATE": "0.25", "FI_PROJECT_TYPE": "observe"}, config_file=str(config))
    assert settings.sample_rate == 0.25


def test_explicit_environment_selects_its_own_config_file(tmp_path, monkeypatch):
    config = tmp_path / "futureagi.toml"
    config.write_text('project_name = "isolated"\n')
    monkeypatch.setenv("FI_CONFIG_FILE", str(tmp_path / "missing.toml"))
    assert resolve(environ={"FI_CONFIG_FILE": str(config)}).project_name == "isolated"


def test_cli_init_config_doctor_validate_bootstrap(tmp_path, monkeypatch, capsys):
    project = tmp_path / "demo-app"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(_stand_ins(tmp_path / "site", anthropic=True)))
    monkeypatch.setattr(_manager, "_state", _manager._State())
    for key in ("FI_API_KEY", "FI_SECRET_KEY", "FI_CONFIG_FILE", "FI_INSTRUMENTATIONS",
                "FI_DISABLED_INSTRUMENTATIONS", "FI_PROJECT_NAME", "OTEL_SDK_DISABLED"):
        monkeypatch.delenv(key, raising=False)
    assert _cli.main(["init-config", "--project", "demo"]) == 0
    assert 'project_name = "demo"' in (project / "futureagi.toml").read_text()
    assert _cli.main(["init-config"]) == 1
    capsys.readouterr()

    loaded = set(sys.modules)
    assert _cli.main(["doctor", "--offline", "--json"]) == 0
    imported = set(sys.modules) - loaded
    report = json.loads(capsys.readouterr().out)
    assert report["config"]["project_name"]["value"] == "demo"
    assert report["collector"] is None
    statuses = {i["name"]: i for i in report["integrations"]}
    assert statuses["openai"]["status"] in ("pending", "instrumented")
    assert statuses["anthropic"]["status"] == "missing_instrumentor"
    assert "not traced" in statuses["anthropic"]["detail"]
    assert "traceAI-anthropic" in statuses["anthropic"]["detail"]
    # doctor only looks: it imports neither the libraries nor their instrumentors.
    assert not imported & {"openai", "traceai_openai", "anthropic"}

    assert _cli.main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "not traced yet" in out
    assert "Set FI_API_KEY and FI_SECRET_KEY" in out
    assert _cli.main(["validate", "--offline"]) == 1
    capsys.readouterr()
    assert _cli.main(["bootstrap"]) == 0
    assert "traceAI-anthropic" in capsys.readouterr().out

    with MockServer() as server:
        monkeypatch.setenv("FI_API_KEY", "fi-api-PLACEHOLDER")
        monkeypatch.setenv("FI_SECRET_KEY", "fi-secret-PLACEHOLDER")
        monkeypatch.setenv("FI_BASE_URL", server.url)
        assert _cli.main(["validate"]) == 0
        assert "accepted a test export" in capsys.readouterr().out
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    assert _cli.main(["validate"]) == 1


def test_cli_run_needs_a_program(capsys):
    assert _cli.main(["run"]) == 2
    assert "give the program to run" in capsys.readouterr().err


def _child_env(stand_ins: Path, server: MockServer, keys: bool = True, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "OTEL_"))}
    env.update({"FI_BASE_URL": server.url, "FI_PROJECT_NAME": "zero-code-app",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                "PYTHONPATH": os.pathsep.join([str(stand_ins), str(PYTHON_ROOT)])})
    if keys:
        env.update({"FI_API_KEY": "fi-api-PLACEHOLDER", "FI_SECRET_KEY": "fi-secret-PLACEHOLDER"})
    env.update(extra)
    return env


def _run(command, env, cwd):
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1], result.stderr


AGENT = [sys.executable, "-m", "fi_instrumentation.auto", sys.executable]


def test_fi_instrument_runs_an_unmodified_app(tmp_path):
    stand_ins = _stand_ins(tmp_path / "site")
    script = tmp_path / "app.py"
    script.write_text(APP)
    with MockServer() as server:
        plain, _ = _run([sys.executable, str(script), server.url], _child_env(stand_ins, server), tmp_path)
        traced, _ = _run(AGENT + [str(script), server.url], _child_env(stand_ins, server), tmp_path)
        spans = decode_spans(server.collected)
        bodies = list(server.collected)
    # The keys travel only in the export's headers, never in what it carries.
    assert not [body for body in bodies if b"fi-api-PLACEHOLDER" in body or b"fi-secret-PLACEHOLDER" in body]
    assert json.loads(plain) == {"replies": ["pong", "pong"], "wire_loaded": False}
    assert traced == plain
    llm = [s for s in spans if s["attributes"].get("gen_ai.span.kind") == "LLM"]
    assert sorted(s["attributes"]["input.value"] for s in llm) == ["ping", "ping again"]
    assert {s["attributes"]["output.value"] for s in llm} == {"pong"}
    assert {s["resource"]["project_name"] for s in spans} == {"zero-code-app"}
    assert {s["resource"]["project_type"] for s in spans} == {"observe"}


def test_app_runs_untouched_without_credentials(tmp_path):
    stand_ins = _stand_ins(tmp_path / "site")
    script = tmp_path / "app.py"
    script.write_text(APP)
    with MockServer() as server:
        plain, _ = _run([sys.executable, str(script), server.url], _child_env(stand_ins, server, keys=False), tmp_path)
        traced, stderr = _run(AGENT + [str(script), server.url], _child_env(stand_ins, server, keys=False), tmp_path)
        assert server.trace_requests() == []
    assert traced == plain
    assert stderr.count(NOT_SET) == 1
    assert stderr.count("traceAI") == 1


def test_sigterm_flushes_and_keeps_the_default_exit(tmp_path):
    stand_ins = _stand_ins(tmp_path / "site")
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    with MockServer() as server:
        env = _child_env(stand_ins, server, OTEL_BSP_SCHEDULE_DELAY="60000")
        proc = subprocess.Popen(AGENT + [str(script), server.url], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=tmp_path)
        try:
            assert proc.stdout.readline().strip() == "ready"
            proc.terminate()
            assert proc.wait(timeout=30) == -signal.SIGTERM
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.stderr.close()
        spans = decode_spans(server.collected)
    assert [s["name"] for s in spans] == ["ChatCompletion"]
