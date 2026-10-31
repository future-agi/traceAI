"""futureagi.toml, precedence, and the fi-instrument command, including a real zero-code run."""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from auto_mock_server import MockServer, decode_spans
from fi_instrumentation.auto import _cli
from fi_instrumentation.auto._config import ConfigError, parse_config_text, resolve

PYTHON_ROOT = Path(__file__).resolve().parents[1]


def test_flat_toml_subset():
    values = parse_config_text(textwrap.dedent('''
        # comment
        project_name = "checkout # agent"   # trailing comment
        instrumentations = ["openai", "wire"]
        wire_capture = false
        sample_rate = 0.25
        wire_max_body_bytes = 1024
        [ignored.table]
        project_name = "not me"
    '''))
    assert values == {
        "project_name": "checkout # agent",
        "instrumentations": ["openai", "wire"],
        "wire_capture": False,
        "sample_rate": 0.25,
        "wire_max_body_bytes": 1024,
    }
    with pytest.raises(ConfigError):
        parse_config_text("not a line")
    with pytest.raises(ConfigError):
        parse_config_text("instrumentations = [1, 2]")


def test_precedence_argument_env_file_default(tmp_path):
    config = tmp_path / "futureagi.toml"
    config.write_text('project_name = "from-file"\nsample_rate = 0.5\ndebug = true\nproject_type = "experiment"\n')
    environ = {"FI_PROJECT_NAME": "from-env", "FI_API_KEY": "abcdefghijkl", "FI_SECRET_KEY": "x"}
    settings = resolve({"project_type": "observe"}, environ=environ, config_file=str(config))
    assert settings.project_name == "from-env"
    assert settings.sample_rate == 0.5
    assert settings.debug is True
    assert settings.project_type == "observe"
    masked = settings.masked()
    assert masked["api_key"]["value"] == "abcd…"
    assert masked["project_name"]["source"] == "env FI_PROJECT_NAME"
    assert masked["sample_rate"]["source"].startswith("file ")
    assert masked["project_type"]["source"] == "argument"
    with pytest.raises(ConfigError):
        resolve(environ={"FI_PROJECT_TYPE": "prod"}, config_file=str(config))
    with pytest.raises(ConfigError):
        resolve(environ={}, config_file=str(tmp_path / "missing.toml"))


def test_cli_init_config_doctor_validate_bootstrap(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for key in ("FI_API_KEY", "FI_SECRET_KEY", "FI_CONFIG_FILE"):
        monkeypatch.delenv(key, raising=False)
    assert _cli.main(["init-config", "--project", "demo"]) == 0
    assert 'project_name = "demo"' in (tmp_path / "futureagi.toml").read_text()
    assert _cli.main(["init-config"]) == 1
    capsys.readouterr()

    assert _cli.main(["doctor", "--offline", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["config"]["project_name"]["value"] == "demo"
    names = {i["name"]: i["status"] for i in report["integrations"]}
    assert names["wire.httpx"] in ("pending", "instrumented")

    assert _cli.main(["doctor", "--offline"]) == 0
    assert "Set FI_API_KEY and FI_SECRET_KEY" in capsys.readouterr().out
    assert _cli.main(["validate", "--offline"]) == 1
    assert _cli.main(["bootstrap"]) == 0
    capsys.readouterr()

    with MockServer() as server:
        monkeypatch.setenv("FI_API_KEY", "key")
        monkeypatch.setenv("FI_SECRET_KEY", "secret")
        monkeypatch.setenv("FI_BASE_URL", server.url)
        assert _cli.main(["validate"]) == 0
        assert "accepted a test export" in capsys.readouterr().out
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    assert _cli.main(["validate"]) == 1


APP = textwrap.dedent('''
    import json, sys, urllib.request
    import requests

    url = sys.argv[1]
    session = requests.Session()
    reply = session.post(url + "/v1/chat/completions",
                         json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}).json()
    stream = session.post(url + "/v1/chat/completions", stream=True,
                          json={"model": "gpt-4o-mini", "stream": True, "messages": [{"role": "user", "content": "go"}]})
    lines = [line for line in stream.iter_lines() if line]
    print(json.dumps({"reply": reply["choices"][0]["message"]["content"], "events": len(lines)}))
''')


def _run_app(tmp_path, server, command, extra_env=None):
    script = tmp_path / "app.py"
    script.write_text(APP)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "OTEL_"))}
    env.update({"FI_API_KEY": "key", "FI_SECRET_KEY": "secret", "FI_BASE_URL": server.url,
                "FI_PROJECT_NAME": "zero-code-app", "PYTHONPATH": str(PYTHON_ROOT)})
    env.update(extra_env or {})
    result = subprocess.run(command + [str(script), server.url], env=env, capture_output=True, text=True,
                            timeout=60, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr


def test_fi_instrument_runs_an_unmodified_app(tmp_path):
    with MockServer() as server:
        output, _ = _run_app(tmp_path, server, [sys.executable, "-m", "fi_instrumentation.auto", sys.executable])
        spans = decode_spans(server.collected)
    assert output == {"reply": "pong", "events": 4}
    names = sorted(s["name"] for s in spans)
    assert names == ["chat gpt-4o-mini", "chat gpt-4o-mini"]
    assert {s["resource"]["project_name"] for s in spans} == {"zero-code-app"}
    assert {s["resource"]["project_type"] for s in spans} == {"observe"}
    streamed = next(s for s in spans if s["attributes"].get("gen_ai.request.stream"))
    assert streamed["attributes"]["output.value"] == "pong"


def test_app_runs_untouched_without_credentials(tmp_path):
    with MockServer() as server:
        output, stderr = _run_app(tmp_path, server, [sys.executable, "-m", "fi_instrumentation.auto", sys.executable],
                                  {"FI_API_KEY": "", "FI_SECRET_KEY": ""})
        assert server.collected == []
    assert output == {"reply": "pong", "events": 4}
    assert stderr.count("FI_API_KEY and FI_SECRET_KEY are not set") == 1


SLEEPER = textwrap.dedent('''
    import sys, time
    import requests

    requests.Session().post(sys.argv[1] + "/v1/chat/completions",
                            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]})
    print("ready", flush=True)
    time.sleep(60)
''')


def test_sigterm_flushes_and_keeps_the_default_exit(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    with MockServer() as server:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "OTEL_"))}
        env.update({"FI_API_KEY": "key", "FI_SECRET_KEY": "secret", "FI_BASE_URL": server.url,
                    "PYTHONPATH": str(PYTHON_ROOT), "OTEL_BSP_SCHEDULE_DELAY": "60000"})
        proc = subprocess.Popen([sys.executable, "-m", "fi_instrumentation.auto", sys.executable, str(script), server.url],
                                env=env, stdout=subprocess.PIPE, text=True, cwd=tmp_path)
        assert proc.stdout.readline().strip() == "ready"
        proc.terminate()
        assert proc.wait(timeout=30) == -signal.SIGTERM
        spans = decode_spans(server.collected)
    assert [s["name"] for s in spans] == ["chat gpt-4o-mini"]


def test_invalid_lower_precedence_settings_do_not_block_override(tmp_path):
    config = tmp_path / "futureagi.toml"
    config.write_text('sample_rate = invalid\nproject_type = invalid\nwire_max_body_bytes = invalid\n')
    settings = resolve({"sample_rate": 0.5, "project_type": "observe", "wire_max_body_bytes": 128},
                       environ={"FI_SAMPLE_RATE": "invalid", "FI_PROJECT_TYPE": "invalid"}, config_file=str(config))
    assert settings.sample_rate == 0.5
    assert settings.project_type == "observe"
    assert settings.wire_max_body_bytes == 128
    settings = resolve(environ={"FI_SAMPLE_RATE": "0.25", "FI_PROJECT_TYPE": "observe", "FI_WIRE_MAX_BODY_BYTES": "64"},
                       config_file=str(config))
    assert settings.sample_rate == 0.25


def test_explicit_environment_selects_its_own_config_file(tmp_path, monkeypatch):
    config = tmp_path / "futureagi.toml"
    config.write_text('project_name = "isolated"\n')
    monkeypatch.setenv("FI_CONFIG_FILE", str(tmp_path / "missing.toml"))
    assert resolve(environ={"FI_CONFIG_FILE": str(config)}).project_name == "isolated"
