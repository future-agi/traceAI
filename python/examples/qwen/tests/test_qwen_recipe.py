"""Qwen recipe contracts: mocked provider traffic and real loopback OTLP exports."""

import atexit
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import fi_instrumentation
import harness
import httpx
import openai
import pytest
import traceai_openai
from harness import Receiver
from opentelemetry import trace
from traceai_openai import OpenAIInstrumentor

import app

RECIPE = Path(__file__).resolve().parents[1]
_fake_spec = importlib.util.spec_from_file_location(
    "qwen_recipe_fake", RECIPE / "tests" / "_fake_openai.py"
)
_fake = importlib.util.module_from_spec(_fake_spec)
_fake_spec.loader.exec_module(_fake)
ANSWER = _fake.ANSWER
AUTH_ERROR = _fake.AUTH_ERROR
MODEL = _fake.MODEL
USAGE = _fake.USAGE
FakeOpenAI = _fake.FakeOpenAI
completion = _fake.completion
stream_body = _fake.stream_body
VENDOR_KEY = "placeholder-qwen-key"
FI_KEY = "placeholder-futureagi-key"
FI_SECRET = "placeholder-futureagi-secret"
DOCUMENTED_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
WORKSPACE = "ws-" + "test0001"  # Syntactic example, never a real workspace.
REGION_BASES = {
    "Beijing": "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "Virginia": "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
    "Singapore": "https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    "Japan (Tokyo)": "https://{WorkspaceId}.ap-northeast-1.maas.aliyuncs.com/compatible-mode/v1",
}
REFUSED_URLS = [
    (REGION_BASES["Beijing"], "workspace id from the console"),
    (REGION_BASES["Singapore"].replace("{WorkspaceId}", "%7BWorkspaceId%7D"), "workspace id from the console"),
    (REGION_BASES["Japan (Tokyo)"].replace("{WorkspaceId}", "%7bWorkspaceId%7d"), "workspace id from the console"),
    (DOCUMENTED_BASE + "?workspace={WorkspaceId}", "workspace id from the console"),
    ("https://dashscope.aliyuncs.com", "/compatible-mode/v1"),
    ("https://dashscope-intl.aliyuncs.com", "/compatible-mode/v1"),
    ("https://cn-hongkong.dashscope.aliyuncs.com", "/compatible-mode/v1"),
    (f"https://{WORKSPACE}.cn-beijing.maas.aliyuncs.com/v1", "/compatible-mode/v1"),
    (DOCUMENTED_BASE + "/chat/completions", "/compatible-mode/v1"),
    (DOCUMENTED_BASE + "//", "/compatible-mode/v1"),
    ("https://aliyuncs.com/v1", "/compatible-mode/v1"),
]
ALLOWED_URLS = [
    DOCUMENTED_BASE,
    DOCUMENTED_BASE + "/",
    REGION_BASES["Virginia"],
    *(base.replace("{WorkspaceId}", WORKSPACE) for base in REGION_BASES.values()),
    f"https://{WORKSPACE}.cn-hongkong.maas.aliyuncs.com/compatible-mode/v1/",
    "http://127.0.0.1:12345/compatible-mode/v1",
    "http://localhost:12345/arbitrary",
    "https://example.invalid/arbitrary",
    "https://aliyuncs.com.example.invalid/arbitrary",
]
TEST_COMMAND = """env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="python/examples/qwen/src:python:python/frameworks/openai:python/tests" \\
  uv run --no-project --python 3.11 \\
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \\
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \\
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \\
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \\
  pytest python/examples/qwen/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs"""


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # Clear inherited tracing settings and proxies without displaying any values.
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_", "DASHSCOPE_", "OPENAI_")) or name.lower() in {
            "http_proxy", "https_proxy", "all_proxy", "no_proxy"
        }:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.MODEL_ENV, MODEL)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(host):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        if host not in ("127.0.0.1", "localhost"):
            raise AssertionError(f"Test refused non-loopback network access to {host}")

    def guarded_dns(host, *args, **kwargs):
        check(host)
        return original_dns(host, *args, **kwargs)

    def guarded_connect(sock, address):
        check(address[0])
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        check(address[0])
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    saved_signals = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    yield
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    for number, handler in saved_signals.items():
        signal.signal(number, handler)


@contextmanager
def trace_session(monkeypatch, **privacy):
    for name, value in privacy.items():
        monkeypatch.setenv(name, str(value).lower())
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        project = f"qwen-recipe-{uuid.uuid4().hex}"
        provider = app.setup_tracing(project)
        try:
            yield receiver, provider, project
        finally:
            OpenAIInstrumentor().uninstrument()
            provider.shutdown()
            atexit.unregister(provider.shutdown)


@pytest.fixture
def traced(monkeypatch):
    with trace_session(monkeypatch) as session:
        yield session


def attributes(span):
    return {
        entry["key"]: next(iter(entry["value"].values()))
        for entry in span.get("attributes", [])
    }


def assert_llm(receiver, project, model=MODEL):
    [span] = receiver.spans()
    attrs = attributes(span)
    assert span["name"] == "ChatCompletion"
    assert attrs["gen_ai.span.kind"] == "LLM"
    if model is None:
        # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
        assert "gen_ai.request.model" not in attrs
    else:
        assert attrs["gen_ai.request.model"] == model
    assert attrs["gen_ai.provider.name"] == "openai"
    for request in receiver.requests():
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == FI_KEY
        assert request["headers"]["x-secret-key"] == FI_SECRET
        [resource] = request["resource_attributes"]
        assert resource["project_name"] == project
        assert resource["project_type"] == "observe"
    assert VENDOR_KEY not in json.dumps([receiver.spans(), receiver.requests()])
    return span, attrs


def assert_usage(attrs):
    assert {name: int(value) for name, value in attrs.items() if name.startswith("gen_ai.usage.")} == {
        "gen_ai.usage.input_tokens": USAGE["prompt_tokens"],
        "gen_ai.usage.output_tokens": USAGE["completion_tokens"],
        "gen_ai.usage.total_tokens": USAGE["total_tokens"],
    }


def assert_vendor_request(request):
    assert str(request.url) == DOCUMENTED_BASE + "/chat/completions"
    assert request.headers["authorization"] == f"Bearer {VENDOR_KEY}"
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    assert FI_KEY not in str(request.headers)
    assert FI_SECRET not in str(request.headers)


def mock_client(body, *, status=200, streaming=False):
    requests = []

    def handler(request):
        requests.append(request)
        if streaming:
            return httpx.Response(status, content=body, headers={"Content-Type": "text/event-stream"})
        response_body = body
        if status == 200 and body.get("object") == "chat.completion":
            response_body = {**body, "model": json.loads(request.content)["model"]}
        return httpx.Response(status, json=response_body)

    client = app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return client, requests


def child_environment(directory, receiver=None, fake=None):
    child_env = os.environ.copy()
    packages = [fi_instrumentation, traceai_openai, harness, openai]
    # Preserve SDK dependencies loaded from an offline cache as well.
    packages.extend(sys.modules[name] for name in ("distro", "sniffio") if name in sys.modules)
    paths = [
        RECIPE / "tests" / "loopback_guard",
        RECIPE / "src",
        *(Path(package.__file__).resolve().parent.parent for package in packages),
    ]
    child_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(str(path) for path in paths))
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["LOOPBACK_GUARD_LOG"] = str(directory / "guard.log")
    child_env["LOOPBACK_GUARD_READY"] = str(directory / "guard.ready")
    if receiver:
        child_env["FI_BASE_URL"] = receiver.origin
    if fake:
        child_env[app.BASE_URL_ENV] = fake.base_url
    return child_env


def test_documented_default_base_url(monkeypatch):
    assert app.DEFAULT_BASE_URL == DOCUMENTED_BASE
    assert DOCUMENTED_BASE in (RECIPE / "README.md").read_text()
    monkeypatch.delenv(app.BASE_URL_ENV, raising=False)
    with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request")))) as client:
        assert str(client.base_url) == DOCUMENTED_BASE + "/"


def test_chat_exports_usage_and_keeps_keys_separate(traced):
    receiver, provider, project = traced
    client, requests = mock_client(completion())
    with client:
        response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Say hello."}])
    assert response.choices[0].message.content == ANSWER
    assert response.model == MODEL
    assert provider.force_flush()
    [request] = requests
    assert_vendor_request(request)
    assert json.loads(request.content)["model"] == MODEL
    span, attrs = assert_llm(receiver, project)
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert attrs["output.value"] == ANSWER
    assert_usage(attrs)


def test_missing_usage_is_omitted(traced):
    receiver, provider, project = traced
    client, _ = mock_client(completion(include_usage=False))
    with client:
        client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Say hello."}])
    assert provider.force_flush()
    _, attrs = assert_llm(receiver, project)
    assert not any(name.startswith("gen_ai.usage.") for name in attrs)


@pytest.mark.parametrize("include_usage", [False, True], ids=["without-usage", "final-chunk-usage"])
def test_stream_accumulates_text_and_optional_usage(traced, include_usage):
    receiver, provider, project = traced
    client, requests = mock_client(stream_body(include_usage=include_usage), streaming=True)
    options = {"stream_options": {"include_usage": True}} if include_usage else {}
    with client:
        stream = client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Say hello."}], stream=True, **options
        )
        chunks = list(stream)
    assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices) == ANSWER
    if include_usage:
        assert chunks[-1].choices == []
        assert chunks[-1].usage.total_tokens == USAGE["total_tokens"]
    assert provider.force_flush()
    [request] = requests
    assert_vendor_request(request)
    body = json.loads(request.content)
    assert body["stream"] is True
    assert body.get("stream_options") == options.get("stream_options")
    span, attrs = assert_llm(receiver, project, model=None)
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert attrs["output.value"] == ANSWER
    if include_usage:
        assert_usage(attrs)
    else:
        assert not any(name.startswith("gen_ai.usage.") for name in attrs)


def test_cross_region_401_records_authentication_error(traced):
    receiver, provider, project = traced
    client, requests = mock_client(AUTH_ERROR, status=401)
    with client, pytest.raises(openai.AuthenticationError) as caught:
        client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Say hello."}])
    assert type(caught.value) is openai.AuthenticationError
    assert caught.value.status_code == 401
    assert caught.value.code == "invalid_api_key"
    assert provider.force_flush()
    [request] = requests
    assert_vendor_request(request)
    span, _ = assert_llm(receiver, project, model=None)
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert "AuthenticationError" in span["status"]["message"]
    [event] = [event for event in span["events"] if event["name"] == "exception"]
    assert attributes(event)["exception.type"] == "openai.AuthenticationError"
    assert "invalid_api_key" in attributes(event)["exception.message"]
    assert VENDOR_KEY not in json.dumps(span)


@pytest.mark.parametrize("hidden", [False, True], ids=["visible-control", "hidden"])
def test_hide_inputs_removes_prompt_from_export(monkeypatch, hidden):
    marker = "qwen-private-prompt-" + uuid.uuid4().hex
    with trace_session(monkeypatch, FI_HIDE_INPUTS=hidden) as (receiver, provider, project):
        client, requests = mock_client(completion())
        with client:
            client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": marker}])
        assert marker in requests[0].content.decode()
        assert provider.force_flush()
        assert_llm(receiver, project)
        assert (marker in json.dumps([receiver.spans(), receiver.requests()])) is (not hidden)


@pytest.mark.parametrize("hidden", [False, True], ids=["visible-control", "hidden"])
def test_hide_outputs_removes_answer_from_export(monkeypatch, hidden):
    with trace_session(monkeypatch, FI_HIDE_OUTPUTS=hidden) as (receiver, provider, project):
        client, _ = mock_client(completion())
        with client:
            response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Say hello."}])
        assert response.choices[0].message.content == ANSWER
        assert provider.force_flush()
        assert_llm(receiver, project)
        assert (ANSWER in json.dumps([receiver.spans(), receiver.requests()])) is (not hidden)


@pytest.mark.parametrize("streaming", [False, True], ids=["chat", "stream"])
def test_app_subprocess_exports_one_span(monkeypatch, streaming):
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="qwen-recipe-") as temporary:
        directory = Path(temporary)
        with Receiver() as receiver, FakeOpenAI() as fake:
            child_env = child_environment(directory, receiver, fake)
            argv = [sys.executable, str(RECIPE / "src" / "app.py"), "--prompt", "Local subprocess prompt."]
            if streaming:
                argv.append("--stream")
            result = subprocess.run(argv, env=child_env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            assert result.stdout == ANSWER + "\n"
            assert all(key not in result.stdout + result.stderr for key in (VENDOR_KEY, FI_KEY, FI_SECRET))
            assert (directory / "guard.ready").read_text() == "installed\n"
            assert (directory / "guard.log").read_text() == ""
            [request] = fake.requests()
            assert request["path"] == "/compatible-mode/v1/chat/completions"
            assert request["headers"]["authorization"] == f"Bearer {VENDOR_KEY}"
            assert "x-api-key" not in request["headers"]
            assert "x-secret-key" not in request["headers"]
            assert request["body"]["model"] == MODEL
            assert request["body"]["messages"] == [{"role": "user", "content": "Local subprocess prompt."}]
            assert request["body"]["stream"] is streaming
            if streaming:
                assert request["body"]["stream_options"] == {"include_usage": True}
            [span] = receiver.spans()
            project = receiver.requests()[0]["resource_attributes"][0]["project_name"]
            assert project == "qwen-openai-recipe"
            _, attrs = assert_llm(receiver, project, model=None if streaming else MODEL)
            assert attrs["output.value"] == ANSWER
            assert_usage(attrs)


@pytest.mark.parametrize("operation", ["getaddrinfo", "connect", "connect_ex"])
def test_guard_refuses_provider_before_dns(operation):
    host = urlsplit(DOCUMENTED_BASE).hostname
    calls = {
        "getaddrinfo": f"socket.getaddrinfo({host!r}, 443)",
        "connect": f"socket.socket().connect(({host!r}, 443))",
        "connect_ex": f"socket.socket().connect_ex(({host!r}, 443))",
    }
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="qwen-guard-") as temporary:
        directory = Path(temporary)
        result = subprocess.run(
            [sys.executable, "-c", "import socket; " + calls[operation]],
            env=child_environment(directory), capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0
        assert "Loopback guard refused" in result.stderr
        assert (directory / "guard.ready").read_text() == "installed\n"
        assert (directory / "guard.log").read_text() == f"{operation}: {host}\n"


@pytest.mark.parametrize("url, reason", REFUSED_URLS)
def test_check_base_url_refuses_documented_mistakes(url, reason):
    with pytest.raises(ValueError) as caught:
        app.check_base_url(url)
    assert reason in str(caught.value)
    assert "\n" not in str(caught.value)


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_check_base_url_preserves_allowed_urls(url):
    assert app.check_base_url(url) == url


@pytest.mark.parametrize("url, reason", [REFUSED_URLS[0], REFUSED_URLS[1], REFUSED_URLS[4]])
def test_main_rejects_url_before_client_or_tracing(monkeypatch, capsys, url, reason):
    with Receiver() as receiver, FakeOpenAI() as fake:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv(app.BASE_URL_ENV, url)
        monkeypatch.delenv(app.API_KEY_ENV, raising=False)

        def forbidden(*_args, **_kwargs):
            pytest.fail("Invalid URL must be rejected before tracing or client setup")

        monkeypatch.setattr(app, "setup_tracing", forbidden)
        monkeypatch.setattr(app, "make_client", forbidden)
        assert app.main([]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert reason in captured.err
        assert fake.requests() == []
        assert receiver.spans() == []
        assert receiver.requests() == []


def test_make_client_env_override_and_explicit_arguments(monkeypatch):
    monkeypatch.setenv(app.BASE_URL_ENV, REGION_BASES["Virginia"])
    with app.make_client() as client:
        assert str(client.base_url) == REGION_BASES["Virginia"] + "/"
        assert client.api_key == VENDOR_KEY
    monkeypatch.setenv(app.BASE_URL_ENV, "https://dashscope.aliyuncs.com")
    monkeypatch.delenv(app.API_KEY_ENV)
    with app.make_client(base_url=DOCUMENTED_BASE, api_key="placeholder-explicit-qwen-key") as client:
        assert str(client.base_url) == DOCUMENTED_BASE + "/"
        assert client.api_key == "placeholder-explicit-qwen-key"
    with pytest.raises(ValueError, match="/compatible-mode/v1"):
        app.make_client()
    monkeypatch.setenv(app.BASE_URL_ENV, "")
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    with app.make_client() as client:
        assert str(client.base_url) == DOCUMENTED_BASE + "/"


def test_make_client_missing_key_names_variable(monkeypatch):
    monkeypatch.delenv(app.API_KEY_ENV)
    with pytest.raises(KeyError) as caught:
        app.make_client()
    assert caught.value.args == ("DASHSCOPE_API_KEY",)


def test_main_missing_model_names_variable_before_tracing(monkeypatch, capsys):
    monkeypatch.delenv(app.MODEL_ENV)
    monkeypatch.setattr(app, "setup_tracing", lambda *_args: pytest.fail("Missing model must fail before tracing"))
    with pytest.raises(SystemExit) as caught:
        app.main([])
    assert caught.value.code == 2
    assert "DASHSCOPE_MODEL" in capsys.readouterr().err


def test_cli_model_overrides_environment():
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="qwen-model-") as temporary:
        directory = Path(temporary)
        with Receiver() as receiver, FakeOpenAI() as fake:
            child_env = child_environment(directory, receiver, fake)
            result = subprocess.run(
                [sys.executable, str(RECIPE / "src" / "app.py"), "--model", "qwen-cli-test"],
                env=child_env, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == ANSWER + "\n"
            [request] = fake.requests()
            assert request["body"]["model"] == "qwen-cli-test"
            assert request["body"]["messages"] == [{"role": "user", "content": "What is a rainbow?"}]
            assert (directory / "guard.ready").exists()
            assert (directory / "guard.log").read_text() == ""
            assert_llm(receiver, "qwen-openai-recipe", model="qwen-cli-test")


def test_setup_tracing_keeps_global_provider(monkeypatch):
    original = trace.get_tracer_provider()
    with trace_session(monkeypatch):
        assert trace.get_tracer_provider() is original
        assert OpenAIInstrumentor().is_instrumented_by_opentelemetry
    assert not OpenAIInstrumentor().is_instrumented_by_opentelemetry


def test_fake_server_error_fixture():
    with FakeOpenAI(error=True) as fake:
        with app.make_client(base_url=fake.base_url) as client:
            with pytest.raises(openai.AuthenticationError) as caught:
                client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Local error."}])
        assert caught.value.status_code == 401
        assert caught.value.code == "invalid_api_key"
        [request] = fake.requests()
        assert request["path"] == "/compatible-mode/v1/chat/completions"
        assert request["headers"]["authorization"] == f"Bearer {VENDOR_KEY}"


def test_readme_and_requirements_pin_contract():
    readme = (RECIPE / "README.md").read_text()
    source = (RECIPE / "src" / "app.py").read_text()
    headings = [
        "## Install", "## Configure", "## Run", "## Code", "## What you see in Future AGI",
        "## Provider specifics", "## Privacy", "## Limits / not covered", "## Tests",
    ]
    positions = [readme.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert readme.startswith("# Qwen (OpenAI-compatible) with traceAI\n")
    assert DOCUMENTED_BASE in readme
    assert "provider field says `openai`" in readme
    for name in (app.API_KEY_ENV, app.BASE_URL_ENV, app.MODEL_ENV, "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"):
        assert name in readme
    for region, base in REGION_BASES.items():
        assert region in readme
        assert base in readme
    for phrase in (
        "Hong Kong", "https://dashscope.aliyuncs.com", "https://dashscope-intl.aliyuncs.com",
        "https://cn-hongkong.dashscope.aliyuncs.com", "https://{WorkspaceId}.cn-hongkong.maas.aliyuncs.com",
        "legacy international (Singapore)", "HTTP 401", "invalid_api_key", "Qwen-Audio",
        "DashScope native", "Responses API", 'stream_options={"include_usage": True}',
        "Without `include_usage`", "1.69.0", "3.24.0", "not tested against the live provider",
        "trailing slash",
        "model id the provider returns in its response", "stream has no model attribute",
        "error span without a model attribute", "current `traceai-openai` behaviour",
    ):
        assert phrase in readme
    assert TEST_COMMAND in readme
    assert not re.search(r"sk-[A-Za-z0-9]{8,}", readme + source)
    assert not re.search(r"ws-[A-Za-z0-9]+", readme + source)
    requirements = (RECIPE / "requirements.txt").read_text().splitlines()
    assert requirements[0].startswith("#")
    assert [line for line in requirements if line and not line.startswith("#")] == [
        "openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0"
    ]
