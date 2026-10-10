"""Offline contract tests for the Baseten recipe and its exported OTLP spans."""

import inspect
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import fi_instrumentation
import httpx
import openai
import pytest
import traceai_openai
from fi_instrumentation import using_session
from harness import Receiver
from traceai_openai import OpenAIInstrumentor

import app
from _fake_openai import ANSWER, ERROR, STREAM_PARTS, USAGE, FakeOpenAI, completion, stream_body

RECIPE = Path(__file__).resolve().parents[1]
ROOT = RECIPE.parents[2]
MODEL = "zai-org/GLM-5.2"
# Synthetic. Not a documented Baseten model id. It exists so the span can be
# told apart from the request slug. No live Baseten call is made.
RESPONSE_MODEL = "fixture-served/not-a-live-baseten-model"
VENDOR_KEY = "placeholder-baseten-key"
FI_KEY = "placeholder-futureagi-api-key"
FI_SECRET = "placeholder-futureagi-secret-key"
DOCUMENTED_URL = "https://inference.baseten.co/v1"
REFUSED = (
    ("https://inference.baseten.co", "Anthropic Messages beta"),
    ("https://inference.baseten.co/", "use https://inference.baseten.co/v1"),
    ("https://model-abc123.api.baseten.co/environments/production/sync/v1",
     "dedicated deployments"),
    # Host spelling variants must not bypass the refusals.
    ("https://inference.baseten.co.", "Anthropic Messages beta"),
    ("https://INFERENCE.Baseten.CO/", "Anthropic Messages beta"),
    ("https://user@inference.baseten.co:443", "Anthropic Messages beta"),
    ("https://model-abc123.api.baseten.co./v1", "dedicated deployments"),
    ("https://MODEL-abc123.API.baseten.co:8443/v1", "dedicated deployments"),
)
TEST_COMMAND = """env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="python/examples/baseten/src:python:python/frameworks/openai:python/tests" \\
  uv run --no-project --python 3.11 \\
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \\
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \\
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \\
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \\
  pytest python/examples/baseten/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs"""


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    """Protect the pytest process too; MockTransport never needs DNS."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check(host):
        assert host in ("127.0.0.1", "localhost", b"127.0.0.1", b"localhost"), (
            "tests must not resolve or connect to an external host"
        )

    def connect(sock, address):
        check(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address[0])
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        check(host)
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    # Remove inherited masking, SDK metadata, collector tuning, and proxies.
    for name in tuple(os.environ):
        if (name.startswith(("FI_", "OPENAI_", "OTEL_", "BASETEN_"))
                or name.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.MODEL_ENV, MODEL)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


@pytest.fixture
def receiver(monkeypatch):
    with Receiver() as sink:
        monkeypatch.setenv("FI_BASE_URL", sink.origin)
        yield sink


@pytest.fixture
def start_tracing(receiver, request):
    providers = []

    def start():
        project = f"baseten-{request.node.name}-{uuid.uuid4().hex}"
        provider = app.setup_tracing(project)
        providers.append(provider)
        return provider, project

    yield start
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    for provider in providers:
        provider.force_flush()
        provider.shutdown()


def attributes(span):
    return {item["key"]: next(iter(item["value"].values()))
            for item in span.get("attributes", [])}


def assert_no_vendor_key(receiver):
    assert VENDOR_KEY not in json.dumps(receiver.spans())
    assert VENDOR_KEY not in json.dumps(receiver.requests())


def one_span(receiver, project=None, model=MODEL):
    spans = receiver.spans()
    assert len(spans) == 1
    span = spans[0]
    attrs = attributes(span)
    assert span["name"] == "ChatCompletion"
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.provider.name"] == "openai"
    requests = receiver.requests()
    assert len(requests) == 1
    export = requests[0]
    assert export["path"] == "/tracer/v1/traces"
    assert export["headers"]["x-api-key"] == FI_KEY
    assert export["headers"]["x-secret-key"] == FI_SECRET
    if project is not None:
        assert export["resource_attributes"][0]["project_name"] == project
    assert export["resource_attributes"][0]["project_type"] == "observe"
    assert_no_vendor_key(receiver)
    if model is None:
        # traceai-openai records the model only from a non-streamed response;
        # flip this when the instrumentor records the request model
        assert "gen_ai.request.model" not in attrs
    else:
        assert attrs["gen_ai.request.model"] == model
    return span, attrs


def chat(client, **kwargs):
    return client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": "Hello fixture"}], **kwargs
    )


def mock_client(handler):
    return app.make_client(http_client=httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False
    ))


def subprocess_env(tmp_path):
    # Follow the imported packages so wheel tests cannot fall back to repo sources.
    paths = [
        str(RECIPE / "tests" / "loopback_guard"),
        str(RECIPE / "src"),
        str(ROOT / "python" / "tests"),
        str(Path(fi_instrumentation.__file__).resolve().parent.parent),
        str(Path(traceai_openai.__file__).resolve().parent.parent),
    ]
    child_env = os.environ.copy()
    child_env.update({
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(paths)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LOOPBACK_GUARD_LOG": str(tmp_path / "guard.log"),
        "LOOPBACK_GUARD_READY": str(tmp_path / "guard.ready"),
    })
    (tmp_path / "guard.log").write_text("", encoding="utf-8")
    return child_env


def test_documented_base_url():
    assert app.DEFAULT_BASE_URL == DOCUMENTED_URL
    assert DOCUMENTED_URL in (RECIPE / "README.md").read_text()
    with app.make_client() as client:
        assert str(client.base_url) == DOCUMENTED_URL + "/"


def test_client_overrides(monkeypatch):
    monkeypatch.setenv(app.BASE_URL_ENV, "http://localhost:1234/proxy")
    with app.make_client() as client:
        assert str(client.base_url) == "http://localhost:1234/proxy/"
    with app.make_client(base_url="http://127.0.0.1:1235/v1",
                         api_key="placeholder-explicit-key") as client:
        assert str(client.base_url) == "http://127.0.0.1:1235/v1/"
        assert client.api_key == "placeholder-explicit-key"
    monkeypatch.setenv(app.BASE_URL_ENV, "")
    with app.make_client() as client:
        assert str(client.base_url) == DOCUMENTED_URL + "/"


def test_missing_vendor_key_names_variable(monkeypatch):
    monkeypatch.delenv(app.API_KEY_ENV)
    with pytest.raises(KeyError, match=app.API_KEY_ENV):
        app.make_client()


def test_chat_export_and_key_separation(receiver, start_tracing):
    provider, project = start_tracing()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion(json.loads(request.content)["model"]))

    with mock_client(handler) as client:
        assert chat(client).choices[0].message.content == ANSWER
    assert provider.force_flush()
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == DOCUMENTED_URL + "/chat/completions"
    assert request.headers["authorization"] == "Bearer " + VENDOR_KEY
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    assert FI_KEY not in str(request.headers)
    assert FI_SECRET not in str(request.headers)
    assert json.loads(request.content)["model"] == MODEL
    _, attrs = one_span(receiver, project)
    assert attrs["gen_ai.usage.input_tokens"] == str(USAGE["prompt_tokens"])
    assert attrs["gen_ai.usage.output_tokens"] == str(USAGE["completion_tokens"])
    assert attrs["gen_ai.usage.total_tokens"] == str(USAGE["total_tokens"])


def test_response_model_differs_from_request(receiver, start_tracing):
    """Span model is the response field, not the request slug."""
    assert RESPONSE_MODEL != MODEL
    provider, project = start_tracing()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion(RESPONSE_MODEL))

    with mock_client(handler) as client:
        response = chat(client)
    assert response.model == RESPONSE_MODEL
    assert provider.force_flush()
    assert json.loads(seen[0].content)["model"] == MODEL
    _, attrs = one_span(receiver, project, model=RESPONSE_MODEL)
    assert attrs["gen_ai.request.model"] != MODEL
    body = completion(RESPONSE_MODEL)
    assert body["object"] == "chat.completion"
    assert body["model"] == RESPONSE_MODEL
    assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert body["choices"][0]["message"]["content"] == ANSWER


def test_usage_absent_is_omitted(receiver, start_tracing):
    provider, project = start_tracing()
    with mock_client(lambda _: httpx.Response(200, json=completion(MODEL, usage=False))) as client:
        chat(client)
    assert provider.force_flush()
    _, attrs = one_span(receiver, project)
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)


def test_stream_accumulates_output(receiver, start_tracing):
    provider, project = start_tracing()
    with mock_client(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=stream_body(MODEL)
    )) as client:
        text = "".join(chunk.choices[0].delta.content or "" for chunk in chat(client, stream=True))
    assert text == "".join(STREAM_PARTS) == ANSWER
    assert provider.force_flush()
    _, attrs = one_span(receiver, project, model=None)
    assert text in attrs["output.value"]
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)
    # The requested model is still exported inside the request parameters.
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == MODEL


def test_authentication_error_is_exported(receiver, start_tracing):
    provider, project = start_tracing()
    with mock_client(lambda _: httpx.Response(401, json=ERROR)) as client:
        with pytest.raises(openai.AuthenticationError, match="Invalid API key"):
            chat(client)
    assert provider.force_flush()
    span, attrs = one_span(receiver, project, model=None)
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == MODEL
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    exceptions = [event for event in span["events"] if event["name"] == "exception"]
    assert exceptions
    assert "Invalid API key" in json.dumps(exceptions)
    assert VENDOR_KEY not in json.dumps(span["status"])
    assert VENDOR_KEY not in json.dumps(exceptions)


@pytest.mark.parametrize("hidden", [False, True], ids=["control", "hidden"])
def test_hide_inputs_has_control(hidden, monkeypatch, receiver, start_tracing):
    marker = "unique-baseten-private-prompt-marker"
    monkeypatch.setenv("FI_HIDE_INPUTS", str(hidden).lower())
    provider, project = start_tracing()
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion(MODEL))

    with mock_client(handler) as client:
        client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": marker}]
        )
    assert provider.force_flush()
    span, _ = one_span(receiver, project)
    assert (marker in json.dumps(span)) is (not hidden)
    assert seen[0]["messages"][0]["content"] == marker


@pytest.mark.parametrize("hidden", [False, True], ids=["control", "hidden"])
def test_hide_outputs(hidden, monkeypatch, receiver, start_tracing):
    monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())
    provider, project = start_tracing()
    with mock_client(lambda _: httpx.Response(200, json=completion(MODEL))) as client:
        assert chat(client).choices[0].message.content == ANSWER
    assert provider.force_flush()
    span, _ = one_span(receiver, project)
    assert (ANSWER in json.dumps(span)) is (not hidden)


def test_session_context_is_independent_of_routing_header(receiver, start_tracing):
    provider, project = start_tracing()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion(MODEL))

    with mock_client(handler) as client:
        chat(client, extra_headers={"x-session-affinity": "routing-1"})
        assert provider.force_flush()
        _, attrs = one_span(receiver, project)
        assert "session.id" not in attrs
        receiver.clear()
        with using_session("s-1"):
            chat(client, extra_headers={"x-session-affinity": "routing-1"})
    assert provider.force_flush()
    _, attrs = one_span(receiver, project)
    assert attrs["session.id"] == "s-1"
    assert len(seen) == 2
    assert all(request.headers["x-session-affinity"] == "routing-1" for request in seen)
    assert all("s-1" not in str(request.headers) for request in seen)


@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
def test_app_subprocess_loopback(stream, receiver, tmp_path):
    with FakeOpenAI() as fake:
        child_env = subprocess_env(tmp_path)
        child_env[app.BASE_URL_ENV] = fake.base_url
        command = [sys.executable, str(RECIPE / "src" / "app.py")]
        if stream:
            command.append("--stream")
        result = subprocess.run(command, env=child_env, cwd=ROOT, capture_output=True,
                                text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ANSWER
        assert VENDOR_KEY not in result.stdout + result.stderr
        _, attrs = one_span(receiver, "baseten-openai", model=None if stream else MODEL)
        if stream:
            assert ANSWER in attrs["output.value"]
            assert not any(key.startswith("gen_ai.usage.") for key in attrs)
        requests = fake.requests()
        assert len(requests) == 1
        assert requests[0]["path"] == "/v1/chat/completions"
        assert requests[0]["headers"]["authorization"] == "Bearer " + VENDOR_KEY
        assert "x-api-key" not in requests[0]["headers"]
        assert "x-secret-key" not in requests[0]["headers"]
        assert requests[0]["body"]["model"] == MODEL
        assert requests[0]["body"]["stream"] is stream
        assert (tmp_path / "guard.log").read_text() == ""
        assert (tmp_path / "guard.ready").read_text() == "loopback guard installed\n"


@pytest.mark.parametrize("operation", ["connect", "connect_ex", "getaddrinfo"])
def test_guard_refuses_provider_before_dns(operation, tmp_path):
    code = {
        "connect": "socket.socket().connect(('inference.baseten.co', 443))",
        "connect_ex": "socket.socket().connect_ex(('inference.baseten.co', 443))",
        "getaddrinfo": "socket.getaddrinfo('inference.baseten.co', 443)",
    }[operation]
    result = subprocess.run([sys.executable, "-c", "import socket; " + code],
                            env=subprocess_env(tmp_path), cwd=ROOT, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode != 0
    assert "loopback guard refused a non-loopback host before DNS" in result.stderr
    assert (tmp_path / "guard.log").read_text() == "blocked non-loopback connection\n"
    assert (tmp_path / "guard.ready").is_file()


@pytest.mark.parametrize("url", [
    DOCUMENTED_URL, DOCUMENTED_URL + "/", "http://127.0.0.1:1234/v1",
    "http://localhost:1234/custom/path/", "https://customer.example/proxy/v1?route=one",
    "https://model-abc123.api.baseten.co.customer.example/v1",
])
def test_allowed_base_urls_are_unchanged(url):
    assert app.check_base_url(url) == url


@pytest.mark.parametrize("url,reason", REFUSED)
def test_out_of_scope_urls_are_refused(url, reason):
    with pytest.raises(ValueError, match=re.escape(reason)) as raised:
        app.check_base_url(url)
    assert "\n" not in str(raised.value)
    with pytest.raises(ValueError, match=re.escape(reason)):
        app.make_client(base_url=url)


@pytest.mark.parametrize("url,reason", REFUSED)
def test_main_rejects_url_before_tracing(url, reason, monkeypatch, receiver, capsys):
    def unexpected_tracing(*_args, **_kwargs):
        pytest.fail("a refused URL must fail before tracing setup")

    def unexpected_client(*_args, **_kwargs):
        pytest.fail("a refused URL must fail before a client is created")

    monkeypatch.setattr(app, "setup_tracing", unexpected_tracing)
    monkeypatch.setattr(app, "make_client", unexpected_client)
    monkeypatch.setattr(app, "OpenAI", unexpected_client)
    monkeypatch.setenv(app.BASE_URL_ENV, url)
    assert app.main([]) == 2
    output = capsys.readouterr()
    assert reason in output.err
    assert output.out == ""
    assert VENDOR_KEY not in output.err
    assert receiver.spans() == []
    assert receiver.requests() == []


@pytest.mark.parametrize("variable", [app.MODEL_ENV, app.API_KEY_ENV])
def test_main_missing_configuration_names_variable(variable, monkeypatch, capsys):
    monkeypatch.delenv(variable)
    monkeypatch.setattr(app, "setup_tracing", lambda: pytest.fail("missing configuration"))
    assert app.main([]) == 2
    output = capsys.readouterr()
    assert variable in output.err
    assert VENDOR_KEY not in output.err


def test_model_and_prompt_cli_override(monkeypatch, receiver, tmp_path):
    with FakeOpenAI() as fake:
        child_env = subprocess_env(tmp_path)
        child_env[app.BASE_URL_ENV] = fake.base_url
        child_env.pop(app.MODEL_ENV)
        result = subprocess.run(
            [sys.executable, str(RECIPE / "src" / "app.py"), "--model", MODEL,
             "--prompt", "A harmless custom prompt"],
            env=child_env, cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ANSWER
        assert fake.requests()[0]["body"]["messages"][0]["content"] == "A harmless custom prompt"
        one_span(receiver, "baseten-openai")
        assert (tmp_path / "guard.log").read_text() == ""
        assert (tmp_path / "guard.ready").is_file()


def test_readme_contract_and_pins():
    readme = (RECIPE / "README.md").read_text()
    assert "# Baseten (OpenAI-compatible) with traceAI" in readme
    headings = ["Install", "Configure", "Run", "Code", "What you see in Future AGI",
                "Provider specifics", "Privacy", "Limits / not covered", "Tests"]
    positions = [readme.index("## " + heading + "\n") for heading in headings]
    assert positions == sorted(positions)
    assert DOCUMENTED_URL in readme
    assert "provider field says `openai`" in readme
    assert "model id the provider returns" in readme
    assert "no `gen_ai.request.model` attribute" in readme
    assert "gen_ai.request.parameters" in readme
    assert "no `gen_ai.usage.*` attributes" in readme
    assert "current `traceai-openai` behaviour" in readme
    for variable in (app.API_KEY_ENV, app.BASE_URL_ENV, app.MODEL_ENV, "FI_API_KEY",
                     "FI_SECRET_KEY", "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"):
        assert variable in readme
    # Literal environment lookups in the app must also be documented.
    for variable in re.findall(r'os\.environ(?:\.get\(|\[)["\']([^"\']+)', inspect.getsource(app)):
        assert variable in readme
    assert TEST_COMMAND in readme
    assert "x-session-affinity" in readme
    assert "NOT a Future AGI" in readme
    assert "using_session" in readme
    assert "1.69.0" in readme
    assert "not tested against the live provider" in readme.lower()
    assert not re.search(r"\b(?:sk-(?:proj-)?|fi-)[A-Za-z0-9_]{20,}\b", readme)
    pins = (RECIPE / "requirements.txt").read_text().splitlines()
    assert pins[0].startswith("#")
    assert pins[1:] == ["openai==3.24.0", "traceAI-openai==0.1.10",
                        "fi-instrumentation-otel==1.1.0"]
