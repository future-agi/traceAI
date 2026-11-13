"""The three zero-code starts against the installed wheel, the real openai SDK and traceAI-openai.

An unmodified app calls a local mock OpenAI server; a local mock collector receives the spans.
These tests need an interpreter where the built fi-instrumentation-otel wheel (which installs the
FI_AUTO_INSTRUMENT .pth and the fi-instrument script), traceAI-openai and openai are installed.
Elsewhere they skip; set TRACEAI_TEST_REQUIRE_WHEEL_E2E=1 to make a missing prerequisite fail.
The opentelemetry-instrument run on a provider OpenTelemetry configured also needs
opentelemetry-distro; set TRACEAI_TEST_REQUIRE_DISTRO_E2E=1 to make its absence fail.
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
# The fake Future AGI keys and the fake model provider key: no export may contain them.
KEYS = (b"fi-api-PLACEHOLDER", b"fi-secret-PLACEHOLDER", b"sk-test-PLACEHOLDER")
# opentelemetry-instrument configures no exporter of its own in these runs.
NO_OTEL_EXPORTERS = {"OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none"}

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

# The app also makes an ordinary HTTP client span of its own, with a credential in the URL and an
# authorization header. It is the application's span, not an AI span.
APP_WITH_OWN_SPAN = textwrap.dedent('''
    import json, sys
    import openai
    from opentelemetry import trace

    client = openai.OpenAI(base_url=sys.argv[1] + "/v1", api_key="sk-test-PLACEHOLDER", max_retries=0)
    attributes = {"url.full": sys.argv[1] + "/orders?api_key=sk-test-PLACEHOLDER",
                  "http.request.header.authorization": ["Bearer sk-test-PLACEHOLDER"]}
    with trace.get_tracer("checkout.http").start_as_current_span("application-only", attributes=attributes):
        response = client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "ping"}])
    print("RESULT " + json.dumps({"reply": response.choices[0].message.content}))
''')


def _site_dirs():
    dirs = list(site.getsitepackages())
    if site.ENABLE_USER_SITE:
        dirs.append(site.getusersitepackages())
    return dirs


def _assert_no_keys(bodies):
    for body in bodies:
        for key in KEYS:
            assert key not in body, key


def _installed_pth_files():
    return [path for path in (Path(d) / "fi_instrumentation_auto.pth" for d in _site_dirs()) if path.is_file()]


def _missing():
    problems = []
    if not _installed_pth_files():
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
           if not k.startswith(("FI_", "OTEL_", "TRACEAI_TEST_")) and k != "PYTHONPATH"}
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
    # The futureagi entry point starts the agent only on the same explicit opt-in.
    return ([str(BIN / "opentelemetry-instrument"), sys.executable, str(script), url],
            {"FI_AUTO_INSTRUMENT": "true", **NO_OTEL_EXPORTERS})


def test_the_wheel_installs_the_start_module_next_to_the_pth():
    # The .pth imports it; without it every FI_AUTO_INSTRUMENT start would print an error.
    assert any((path.parent / "fi_instrumentation_auto.py").is_file() for path in _installed_pth_files())


@pytest.mark.parametrize("start", ["fi-instrument", "FI_AUTO_INSTRUMENT", "opentelemetry-instrument"])
def test_each_start_traces_an_unmodified_app(tmp_path, start):
    script = tmp_path / "app.py"
    script.write_text(APP)
    with MockServer() as server:
        plain, _ = _run([sys.executable, str(script), server.url], _env(server), tmp_path)
        command, extra = _start(start, script, server.url)
        traced, _ = _run(command, _env(server, **extra), tmp_path)
        spans = decode_spans(server.collected)
        bodies = list(server.collected)
    assert json.loads(plain) == {"replies": ["pong", "pong"], "wire_loaded": False}
    assert traced == plain
    llm = [s for s in spans if s["attributes"].get("gen_ai.span.kind") == "LLM"]
    assert len(llm) == 2, [s["name"] for s in spans]
    assert {s["resource"]["project_name"] for s in llm} == {"zero-code-e2e"}
    assert {s["resource"]["project_type"] for s in llm} == {"observe"}
    _assert_no_keys(bodies)


def test_opentelemetry_instrument_with_keys_alone_exports_nothing(tmp_path):
    # Keys set for a manual setup elsewhere in the app are not an opt-in to the agent.
    script = tmp_path / "app.py"
    script.write_text(APP)
    with MockServer() as server:
        plain, _ = _run([sys.executable, str(script), server.url], _env(server), tmp_path)
        traced, _ = _run([str(BIN / "opentelemetry-instrument"), sys.executable, str(script), server.url],
                         _env(server, **NO_OTEL_EXPORTERS), tmp_path)
        assert server.trace_requests() == []
    assert traced == plain


def test_opentelemetry_instrument_on_a_configured_provider_sends_only_ai_spans(tmp_path):
    if importlib.util.find_spec("opentelemetry.distro") is None:
        message = "opentelemetry-distro is not installed in this interpreter"
        if os.environ.get("TRACEAI_TEST_REQUIRE_DISTRO_E2E") == "1":
            pytest.fail(message)
        pytest.skip(message)
    script = tmp_path / "app.py"
    script.write_text(APP_WITH_OWN_SPAN)
    with MockServer() as server:
        # The distro configures the application's provider, here with a console exporter.
        env = _env(server, FI_AUTO_INSTRUMENT="true", OTEL_SERVICE_NAME="checkout", OTEL_TRACES_EXPORTER="console",
                   OTEL_METRICS_EXPORTER="none", OTEL_LOGS_EXPORTER="none")
        result = subprocess.run([str(BIN / "opentelemetry-instrument"), sys.executable, str(script), server.url],
                                env=env, capture_output=True, text=True, timeout=180, cwd=tmp_path)
        spans = decode_spans(server.collected)
        bodies = list(server.collected)
    assert result.returncode == 0, result.stderr
    assert 'RESULT {"reply": "pong"}' in result.stdout
    # The application's own exporter still gets its own span.
    assert '"application-only"' in result.stdout
    llm = [s for s in spans if s["attributes"].get("gen_ai.span.kind") == "LLM"]
    assert len(llm) == 1, [s["name"] for s in spans]
    assert "application-only" not in [s["name"] for s in spans]
    # The Future AGI route ran on the provider OpenTelemetry configured, so it keeps the app's service name.
    assert {s["resource"].get("service.name") for s in spans} == {"checkout"}
    assert {s["resource"]["project_name"] for s in spans} == {"zero-code-e2e"}
    _assert_no_keys(bodies)


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
