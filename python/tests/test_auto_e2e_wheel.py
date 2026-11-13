"""The three zero-code starts against the installed wheel, the real openai SDK and traceAI-openai.

An unmodified app calls a local mock OpenAI server; a local mock collector receives the spans.
These tests need an interpreter where the built fi-instrumentation-otel wheel (which installs the
FI_AUTO_INSTRUMENT .pth and the fi-instrument script), traceAI-openai and openai are installed.
Elsewhere they skip; set TRACEAI_TEST_REQUIRE_WHEEL_E2E=1 to make a missing prerequisite fail.
"""

import importlib.util
import json
import os
import signal
import site
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from auto_mock_server import MockServer, decode_spans

BIN = Path(sys.executable).parent
REQUIRED = os.environ.get("TRACEAI_TEST_REQUIRE_WHEEL_E2E") == "1"
NOT_SET = "FI_API_KEY and FI_SECRET_KEY are not set"

APP = textwrap.dedent('''
    import json, sys
    import openai

    client = openai.OpenAI(base_url=sys.argv[1] + "/v1", api_key="sk-test-PLACEHOLDER", max_retries=0)
    replies = []
    for prompt in ("ping", "ping again"):
        response = client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        replies.append(response.choices[0].message.content)
    wire = any(name.startswith("fi_instrumentation.auto.wire") for name in sys.modules)
    print(json.dumps({"replies": replies, "wire_loaded": wire}))
''')

SLEEPER = textwrap.dedent('''
    import sys, time
    import openai

    client = openai.OpenAI(base_url=sys.argv[1] + "/v1", api_key="sk-test-PLACEHOLDER", max_retries=0)
    client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "ping"}])
    print("ready", flush=True)
    time.sleep(60)
''')


def _site_dirs():
    dirs = list(site.getsitepackages())
    if site.ENABLE_USER_SITE:
        dirs.append(site.getusersitepackages())
    return dirs


def _missing():
    problems = []
    if not any((Path(d) / "fi_instrumentation_auto.pth").is_file() for d in _site_dirs()):
        problems.append("the fi-instrumentation-otel wheel is not installed in this interpreter")
    for module in ("openai", "traceai_openai"):
        if importlib.util.find_spec(module) is None:
            problems.append(f"{module} is not installed")
    for script in ("fi-instrument", "opentelemetry-instrument"):
        if not (BIN / script).exists():
            problems.append(f"{script} is not installed next to {sys.executable}")
    return problems


@pytest.fixture(scope="module", autouse=True)
def _prerequisites():
    problems = _missing()
    if problems:
        message = "zero-code wheel run not possible here: " + "; ".join(problems)
        if REQUIRED:
            pytest.fail(message)
        pytest.skip(message)


def _env(server=None, keys=True, **extra):
    # The children import the installed wheel, never the source tree.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FI_", "OTEL_")) and k not in ("PYTHONPATH", "TRACEAI_TEST_REQUIRE_WHEEL_E2E")}
    env.update({"FI_PROJECT_NAME": "zero-code-e2e", "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost"})
    if server is not None:
        env["FI_BASE_URL"] = server.url
    if keys:
        env.update({"FI_API_KEY": "fi-api-PLACEHOLDER", "FI_SECRET_KEY": "fi-secret-PLACEHOLDER"})
    env.update(extra)
    return env


def _run(command, env, cwd):
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1], result.stderr


def _start(name, script, url):
    if name == "fi-instrument":
        return [str(BIN / "fi-instrument"), sys.executable, str(script), url], {}
    if name == "FI_AUTO_INSTRUMENT":
        return [sys.executable, str(script), url], {"FI_AUTO_INSTRUMENT": "true"}
    return [str(BIN / "opentelemetry-instrument"), sys.executable, str(script), url], {}


@pytest.mark.parametrize("start", ["fi-instrument", "FI_AUTO_INSTRUMENT", "opentelemetry-instrument"])
def test_each_start_traces_an_unmodified_app(tmp_path, start):
    script = tmp_path / "app.py"
    script.write_text(APP)
    with MockServer() as server:
        plain, _ = _run([sys.executable, str(script), server.url], _env(server), tmp_path)
        command, extra = _start(start, script, server.url)
        traced, _ = _run(command, _env(server, **extra), tmp_path)
        spans = decode_spans(server.collected)
    assert json.loads(plain) == {"replies": ["pong", "pong"], "wire_loaded": False}
    assert traced == plain
    llm = [s for s in spans if s["attributes"].get("gen_ai.span.kind") == "LLM"]
    assert len(llm) == 2, [s["name"] for s in spans]
    assert {s["resource"]["project_name"] for s in llm} == {"zero-code-e2e"}
    assert {s["resource"]["project_type"] for s in llm} == {"observe"}


def test_without_keys_the_app_runs_untouched_and_warns_once(tmp_path):
    script = tmp_path / "app.py"
    script.write_text(APP)
    with MockServer() as server:
        plain, _ = _run([sys.executable, str(script), server.url], _env(server, keys=False), tmp_path)
        traced, stderr = _run([str(BIN / "fi-instrument"), sys.executable, str(script), server.url],
                              _env(server, keys=False), tmp_path)
        assert server.trace_requests() == []
    assert traced == plain
    assert stderr.count(NOT_SET) == 1


def test_doctor_json_lists_installed_libraries(tmp_path):
    # A stand-in anthropic library with no traceAI package next to it.
    stand_in = tmp_path / "site" / "anthropic"
    stand_in.mkdir(parents=True)
    (stand_in / "__init__.py").write_text("")
    env = _env(keys=False, PYTHONPATH=str(tmp_path / "site"))
    result = subprocess.run([str(BIN / "fi-instrument"), "doctor", "--offline", "--json"], env=env,
                            capture_output=True, text=True, timeout=180, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    statuses = {i["name"]: i for i in json.loads(result.stdout)["integrations"]}
    assert statuses["openai"]["status"] in ("pending", "instrumented")
    assert statuses["anthropic"]["status"] == "missing_instrumentor"
    assert "not traced" in statuses["anthropic"]["detail"]


def test_sigterm_flushes_and_keeps_the_default_exit(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    with MockServer() as server:
        env = _env(server, OTEL_BSP_SCHEDULE_DELAY="60000")
        proc = subprocess.Popen([str(BIN / "fi-instrument"), sys.executable, str(script), server.url], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path)
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
    assert len([s for s in spans if s["attributes"].get("gen_ai.span.kind") == "LLM"]) == 1
