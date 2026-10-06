"""Recipe contracts: mocked vendor traffic and guarded loopback subprocesses."""

import json
import os
import re
import socket
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import app
import fi_instrumentation
import harness
import httpx
import openai
import pytest
import traceai_openai
from harness import Receiver
from opentelemetry import trace
from traceai_openai import OpenAIInstrumentor

from _fake_openai import (
    ANSWER,
    CHAT_USAGE,
    RAW_MARKER,
    REQUEST_MODEL,
    RESPONSE_MODEL,
    STREAM_USAGE,
    FakeOpenAI,
    completion,
    error_body,
    stream_body,
)

RECIPE = Path(__file__).resolve().parents[1]
VENDOR_KEY = "placeholder-anannas-key"
FI_KEY = "placeholder-fi-api-key"
FI_SECRET = "placeholder-fi-secret-key"
VENDOR_ENDPOINT = "https://api.anannas.ai/v1/chat/completions"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # Ignore inherited tracing configuration and credentials without printing them.
    for name in tuple(os.environ):
        if name.startswith(("FI_", "ANANNAS_", "OTEL_", "OPENAI_")) or name.lower() in {
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        }:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.MODEL_ENV, REQUEST_MODEL)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    # Protect the pytest process as well as the subprocesses, even on a regression.
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(host):
        if host not in ("127.0.0.1", "localhost"):
            raise OSError("recipe tests allow loopback only")

    def dns(host, *args, **kwargs):
        check(host)
        return original_dns(host, *args, **kwargs)

    def connect(sock, address):
        check(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address[0])
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()


@pytest.fixture
def receiver(monkeypatch):
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        yield receiver


@contextmanager
def instrumented():
    project = "anannas-recipe-" + uuid.uuid4().hex
    provider = app.setup_tracing(project)
    try:
        yield provider, project
    finally:
        OpenAIInstrumentor().uninstrument()
        provider.force_flush()
        provider.shutdown()


def attributes(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def assert_vendor_request(request, *, endpoint=VENDOR_ENDPOINT, model=REQUEST_MODEL, key=VENDOR_KEY):
    assert str(request.url) == endpoint
    assert request.headers["authorization"] == f"Bearer {key}"
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    recorded = json.dumps({"headers": dict(request.headers), "body": json.loads(request.content)})
    assert FI_KEY not in recorded and FI_SECRET not in recorded
    assert json.loads(request.content)["model"] == model


def assert_export(receiver, project):
    [span] = receiver.spans()
    attrs = attributes(span)
    assert span["name"] == "ChatCompletion"
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.provider.name"] == "openai"
    [request] = receiver.requests()
    assert request["path"] == "/tracer/v1/traces"
    assert request["headers"]["x-api-key"] == FI_KEY
    assert request["headers"]["x-secret-key"] == FI_SECRET
    assert "authorization" not in request["headers"]
    [resource] = request["resource_attributes"]
    assert resource["project_name"] == project
    assert resource["project_type"] == "observe"
    # Positive control: each caller also asserts the bearer on its vendor request.
    assert VENDOR_KEY not in json.dumps({"spans": receiver.spans(), "exports": receiver.requests()})
    return span, attrs


def assert_parameters(attrs, *, stream=False):
    params = json.loads(attrs["gen_ai.request.parameters"])
    assert params["model"] == REQUEST_MODEL
    assert params.get("stream", False) is stream


def assert_usage(attrs, expected):
    assert {key: int(value) for key, value in attrs.items() if key.startswith("gen_ai.usage.")} == {
        "gen_ai.usage.input_tokens": expected["prompt_tokens"],
        "gen_ai.usage.output_tokens": expected["completion_tokens"],
        "gen_ai.usage.total_tokens": expected["total_tokens"],
    }


def mock_client(handler):
    return app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_documented_default_and_sdk_trailing_slash():
    assert app.DEFAULT_BASE_URL == "https://api.anannas.ai/v1"
    assert app.API_KEY_ENV == "ANANNAS_API_KEY"
    assert app.BASE_URL_ENV == "ANANNAS_BASE_URL"
    assert app.MODEL_ENV == "ANANNAS_MODEL"
    assert "https://api.anannas.ai/v1" in (RECIPE / "README.md").read_text()
    with mock_client(lambda request: pytest.fail("client construction must not make a request")) as client:
        assert str(client.base_url) == "https://api.anannas.ai/v1/"


def test_client_environment_and_explicit_overrides(monkeypatch):
    monkeypatch.setenv(app.BASE_URL_ENV, "https://proxy.example.test/customer/v1/")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion())

    with mock_client(handler) as client:
        assert str(client.base_url) == "https://proxy.example.test/customer/v1/"
        client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "Proxy fixture."}])
    assert_vendor_request(seen.pop(), endpoint="https://proxy.example.test/customer/v1/chat/completions")
    with app.make_client(base_url="https://anannas.ai/v1/", api_key="placeholder-explicit-anannas-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "Explicit override."}])
    assert_vendor_request(seen.pop(), endpoint="https://anannas.ai/v1/chat/completions", key="placeholder-explicit-anannas-key")
    assert seen == []


def test_make_client_missing_key_names_variable(monkeypatch):
    monkeypatch.delenv(app.API_KEY_ENV)
    with pytest.raises(KeyError) as error:
        app.make_client()
    assert error.value.args == ("ANANNAS_API_KEY",)


def test_chat_pins_response_model_usage_and_key_separation(receiver):
    seen = []
    global_provider = trace.get_tracer_provider()

    def handler(request):
        assert_vendor_request(request)
        seen.append(request)
        return httpx.Response(200, json=completion())

    with instrumented() as (provider, project):
        assert trace.get_tracer_provider() is global_provider
        with mock_client(handler) as client:
            response = client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "Chat control prompt."}])
        assert response.choices[0].message.content == ANSWER
        provider.force_flush()
        span, attrs = assert_export(receiver, project)
    assert len(seen) == 1
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert RESPONSE_MODEL != REQUEST_MODEL
    assert attrs["gen_ai.request.model"] == "openai/gpt-5-mini-2026-09-01"
    assert_parameters(attrs)
    assert_usage(attrs, CHAT_USAGE)
    assert "Chat control prompt." in json.dumps(span)


def test_missing_usage_is_omitted(receiver):
    seen = []

    def handler(request):
        assert_vendor_request(request)
        seen.append(request)
        return httpx.Response(200, json=completion(usage=False))

    with instrumented() as (provider, project):
        with mock_client(handler) as client:
            response = client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "No usage fixture."}])
        assert response.usage is None
        provider.force_flush()
        _, attrs = assert_export(receiver, project)
    assert len(seen) == 1
    assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)


@pytest.mark.parametrize("include_usage", [False, True], ids=["default", "final-usage-chunk"])
def test_stream_pins_text_model_gap_and_usage(receiver, include_usage):
    seen = []

    def handler(request):
        assert_vendor_request(request)
        body = json.loads(request.content)
        assert body["stream"] is True
        if include_usage:
            assert body["stream_options"] == {"include_usage": True}
        else:
            assert "stream_options" not in body
        seen.append(request)
        return httpx.Response(200, content=stream_body(include_usage=include_usage), headers={"Content-Type": "text/event-stream"})

    with instrumented() as (provider, project):
        with mock_client(handler) as client:
            kwargs = {"stream_options": {"include_usage": True}} if include_usage else {}
            chunks = list(client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "Stream control prompt."}], stream=True, **kwargs))
        assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices) == ANSWER
        provider.force_flush()
        span, attrs = assert_export(receiver, project)
    assert len(seen) == 1
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert attrs["output.value"] == ANSWER
    # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
    assert "gen_ai.request.model" not in attrs
    assert_parameters(attrs, stream=True)
    if include_usage:
        assert_usage(attrs, STREAM_USAGE)
        assert chunks[-1].usage.total_tokens == 24
    else:
        assert not any(key.startswith("gen_ai.usage.") for key in attrs)


@pytest.mark.parametrize("status, error_class", [(401, openai.AuthenticationError), (402, openai.APIStatusError)], ids=["401-authentication", "402-credits"])
def test_errors_pin_sdk_class_exception_and_model_gap(receiver, status, error_class):
    seen = []

    def handler(request):
        assert_vendor_request(request)
        seen.append(request)
        return httpx.Response(status, json=error_body(status))

    with instrumented() as (provider, project):
        with mock_client(handler) as client, pytest.raises(error_class) as error:
            client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "Error fixture prompt."}])
        assert type(error.value) is error_class
        assert error.value.status_code == status
        assert VENDOR_KEY not in str(error.value)
        provider.force_flush()
        span, attrs = assert_export(receiver, project)
    assert len(seen) == 1
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert error_class.__name__ in span["status"]["message"]
    [exception] = [event for event in span["events"] if event["name"] == "exception"]
    assert error_body(status)["error"]["message"] in json.dumps(exception)
    # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
    assert "gen_ai.request.model" not in attrs
    assert_parameters(attrs)
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)


def test_chat_output_is_assistant_content_without_raw_response(receiver):
    def handler(request):
        assert_vendor_request(request)
        return httpx.Response(200, json=completion())

    with instrumented() as (provider, project):
        with mock_client(handler) as client:
            response = client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "Raw marker control."}])
        assert response.fixture_marker == RAW_MARKER  # Prove the fixture carried extra data.
        provider.force_flush()
        span, attrs = assert_export(receiver, project)
    assert attrs["output.value"] == ANSWER
    assert ANSWER in json.dumps(span)
    assert RAW_MARKER not in json.dumps(span)


def test_no_text_stream_exports_empty_output_without_raw_fallback(receiver):
    def handler(request):
        assert_vendor_request(request)
        return httpx.Response(200, content=stream_body(no_text=True), headers={"Content-Type": "text/event-stream"})

    with instrumented() as (provider, project):
        with mock_client(handler) as client:
            chunks = list(client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "No text stream control."}], stream=True))
        assert chunks[0].fixture_marker == RAW_MARKER
        assert all(not chunk.choices[0].delta.content for chunk in chunks)
        provider.force_flush()
        span, attrs = assert_export(receiver, project)
    assert attrs["output.value"] == ""
    assert RAW_MARKER not in json.dumps(span)
    assert "No text stream control." in json.dumps(span)
    assert "gen_ai.request.model" not in attrs
    assert_parameters(attrs, stream=True)


@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
@pytest.mark.parametrize("hidden", ["inputs", "outputs", "both"])
def test_privacy_flags_have_unmasked_controls(receiver, monkeypatch, stream, hidden):
    prompt = "privacy-prompt-" + uuid.uuid4().hex
    answer = "privacy-answer-" + uuid.uuid4().hex
    for masking in (False, True):
        receiver.clear()
        hide_inputs = masking and hidden in ("inputs", "both")
        hide_outputs = masking and hidden in ("outputs", "both")
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hide_inputs).lower())
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hide_outputs).lower())
        seen = []

        def handler(request):
            assert_vendor_request(request)
            assert json.loads(request.content)["messages"][0]["content"] == prompt
            seen.append(request)
            if stream:
                return httpx.Response(200, content=stream_body(answer=answer), headers={"Content-Type": "text/event-stream"})
            return httpx.Response(200, json=completion(answer=answer))

        with instrumented() as (provider, project):
            with mock_client(handler) as client:
                response = client.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": prompt}], stream=stream)
                observed = "".join(chunk.choices[0].delta.content or "" for chunk in response) if stream else response.choices[0].message.content
            assert observed == answer  # Tracing masks never change the provider response.
            provider.force_flush()
            span, attrs = assert_export(receiver, project)
        assert len(seen) == 1
        exported = json.dumps(span)
        if hide_inputs:
            assert prompt not in exported
            assert attrs["input.value"] == "__REDACTED__"
            assert not any(key.startswith("gen_ai.input.messages.") for key in attrs)
        else:
            assert prompt in exported
        if hide_outputs:
            assert answer not in exported
            assert attrs["output.value"] == "__REDACTED__"
            assert not any(key.startswith("gen_ai.output.messages.") for key in attrs)
        else:
            assert answer in exported
            assert attrs["output.value"] == answer
        assert_parameters(attrs, stream=stream)


def allowed_urls():
    for host in ("api.anannas.ai", "anannas.ai", "API.ANANNAS.AI", "ANANNAS.AI.", "api.anannas.ai."):
        for path in ("/v1", "/v1/"):
            yield f"https://{host}{path}"
    yield "HTTPS://API.ANANNAS.AI/v1/"
    yield "https://proxy.example.test/custom/chat?route=fixture#local"
    yield "http://proxy.example.test/customer/"
    yield "http://127.0.0.1:12345/v1"
    yield "http://localhost:12345/customer/v1/"


def test_allowed_urls_are_returned_unchanged():
    for url in allowed_urls():
        returned = app.check_base_url(url)
        assert returned is url


def refused_urls():
    paths = ("", "/", "/v1/chat/completions", "/chat/completions", "/v1/models", "/v2", "/v1//", "/%76%31", "/v1%2F", "/v1/../models")
    for host in ("api.anannas.ai", "anannas.ai", "API.ANANNAS.AI.", "ANANNAS.AI."):
        for path in paths:
            yield f"https://{host}{path}", "base path"
        yield f"http://{host}/v1", "HTTPS"
        for delimiter in ("?", "#", "?route=fixture", "#fixture"):
            yield f"https://{host}/v1{delimiter}", "query and fragment"
    for char in (" ", "\t", "\r", "\n", "\x00", "\x7f", "\u00a0", "\u200b"):
        yield f"https://api.anannas.ai{char}/v1", "whitespace or control"
    for host in ("api\u3002anannas.ai", "api\uff0eanannas.ai", "api\uff61anannas.ai", "\u00e1pi.anannas.ai", "xn--.anannas.ai", "xn--invalid-.anannas.ai"):
        yield f"https://{host}/v1", "ASCII host"
    for host in ("api.anannas.ai", "anannas.ai", "proxy.example.test", "127.0.0.1:12345"):
        yield f"https://placeholder-url-user:placeholder-url-password@{host}/v1", "credentials"
    yield "https://@api.anannas.ai/v1", "credentials"
    yield "not-a-url", "absolute HTTP(S)"
    yield "https:///v1", "absolute HTTP(S)"
    yield "ftp://api.anannas.ai/v1", "absolute HTTP(S)"
    yield "https://api.anannas.ai:invalid/v1", "valid HTTP(S)"
    yield "https://api.anannas.ai:99999/v1", "valid HTTP(S)"
    yield "https://[api.anannas.ai/v1", "valid HTTP(S)"


def test_every_refused_url_exits_before_tracing_and_requests(receiver, monkeypatch, capsys):
    def forbidden_setup(*args, **kwargs):
        pytest.fail("refused configuration must exit before setup_tracing")

    monkeypatch.setattr(app, "setup_tracing", forbidden_setup)
    with FakeOpenAI() as fake:
        for url, reason in refused_urls():
            with pytest.raises(ValueError) as error:
                app.check_base_url(url)
            message = str(error.value)
            assert app.BASE_URL_ENV in message
            assert reason in message
            assert "\n" not in message and "\r" not in message
            if reason == "base path":
                assert "https://api.anannas.ai/v1" in message
            # OS environment variables cannot hold NUL. An in-memory environment
            # exercises main() with every spelling, including that control byte.
            monkeypatch.setattr(app, "os", SimpleNamespace(environ={
                app.BASE_URL_ENV: url,
                app.MODEL_ENV: REQUEST_MODEL,
                app.API_KEY_ENV: VENDOR_KEY,
            }))
            assert app.main([]) == 2
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == message + "\n"
            assert "placeholder-url-user" not in captured.err
            assert "placeholder-url-password" not in captured.err
            assert VENDOR_KEY not in captured.err
            assert fake.requests() == []
            assert receiver.spans() == []
            assert receiver.requests() == []


@pytest.mark.parametrize("variable", ["ANANNAS_MODEL", "ANANNAS_API_KEY"])
@pytest.mark.parametrize("value", [None, ""], ids=["missing", "empty"])
def test_missing_model_or_key_exits_before_tracing(receiver, monkeypatch, capsys, variable, value):
    def forbidden_setup(*args, **kwargs):
        pytest.fail("missing configuration must exit before setup_tracing")

    monkeypatch.setattr(app, "setup_tracing", forbidden_setup)
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)
    with FakeOpenAI() as fake:
        monkeypatch.setenv(app.BASE_URL_ENV, fake.base_url)
        assert app.main([]) == 2
        captured = capsys.readouterr()
        assert variable in captured.err
        assert captured.out == ""
        assert VENDOR_KEY not in captured.err
        assert fake.requests() == []
        assert receiver.spans() == []
        assert receiver.requests() == []


def child_environment(tmp_path):
    environment = os.environ.copy()
    # Use the actual imported package locations; this also works with wheels.
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(str(path) for path in (
        RECIPE / "tests" / "loopback_guard",
        RECIPE / "src",
        Path(fi_instrumentation.__file__).resolve().parent.parent,
        Path(traceai_openai.__file__).resolve().parent.parent,
        Path(harness.__file__).resolve().parent.parent,
    )))
    environment["LOOPBACK_GUARD_LOG"] = str(tmp_path / "guard.log")
    environment["LOOPBACK_GUARD_READY"] = str(tmp_path / "guard.ready")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def assert_guard_ready(environment, *, blocked=None):
    assert Path(environment["LOOPBACK_GUARD_READY"]).read_text() == "loopback guard installed\n"
    assert Path(environment["LOOPBACK_GUARD_LOG"]).read_text() == (f"blocked {blocked}\n" if blocked else "")


def assert_fake_request(fake, *, stream=False, prompt=None):
    [request] = fake.requests()
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == f"Bearer {VENDOR_KEY}"
    assert "x-api-key" not in request["headers"]
    assert "x-secret-key" not in request["headers"]
    assert FI_KEY not in json.dumps(request) and FI_SECRET not in json.dumps(request)
    assert request["body"]["model"] == REQUEST_MODEL
    assert request["body"].get("stream", False) is stream
    if prompt is not None:
        assert request["body"]["messages"] == [{"role": "user", "content": prompt}]


@pytest.mark.parametrize("mode", ["chat", "stream", "cli-model"])
def test_app_subprocess_chat_and_stream(receiver, monkeypatch, tmp_path, mode):
    project = "anannas-subprocess-" + uuid.uuid4().hex
    monkeypatch.setenv("FI_PROJECT_NAME", project)
    environment = child_environment(tmp_path)
    prompt = "Subprocess fixture prompt."
    arguments = [sys.executable, str(RECIPE / "src" / "app.py"), "--prompt", prompt]
    if mode == "stream":
        arguments.append("--stream")
    if mode == "cli-model":
        environment[app.MODEL_ENV] = "fixture/ignored-model"
        arguments.extend(["--model", REQUEST_MODEL])
    with FakeOpenAI() as fake:
        environment[app.BASE_URL_ENV] = fake.base_url
        result = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert result.stdout.endswith(ANSWER + "\n")
        assert all(key not in result.stdout + result.stderr for key in (VENDOR_KEY, FI_KEY, FI_SECRET))
        assert_fake_request(fake, stream=mode == "stream", prompt=prompt)
    assert_guard_ready(environment)
    _, attrs = assert_export(receiver, project)
    assert attrs["output.value"] == ANSWER
    assert_parameters(attrs, stream=mode == "stream")
    if mode == "stream":
        assert "gen_ai.request.model" not in attrs
        assert not any(key.startswith("gen_ai.usage.") for key in attrs)
    else:
        assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
        assert_usage(attrs, CHAT_USAGE)


@pytest.mark.parametrize("status, error_name", [(401, "AuthenticationError"), (402, "APIStatusError")])
def test_app_subprocess_error_flushes_span(receiver, monkeypatch, tmp_path, status, error_name):
    project = "anannas-subprocess-error-" + uuid.uuid4().hex
    monkeypatch.setenv("FI_PROJECT_NAME", project)
    environment = child_environment(tmp_path)
    with FakeOpenAI(status=status) as fake:
        environment[app.BASE_URL_ENV] = fake.base_url
        result = subprocess.run([sys.executable, str(RECIPE / "src" / "app.py")], env=environment, capture_output=True, text=True, timeout=120)
        assert result.returncode == 1
        assert error_name in result.stderr
        assert all(key not in result.stdout + result.stderr for key in (VENDOR_KEY, FI_KEY, FI_SECRET))
        assert_fake_request(fake)
    assert_guard_ready(environment)
    span, attrs = assert_export(receiver, project)
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert any(event["name"] == "exception" for event in span["events"])
    assert "gen_ai.request.model" not in attrs
    assert_parameters(attrs)


@pytest.mark.parametrize("operation, expression", [
    ("getaddrinfo", "socket.getaddrinfo('api.anannas.ai', 443)"),
    ("connect", "socket.socket().connect(('api.anannas.ai', 443))"),
    ("connect_ex", "socket.socket().connect_ex(('api.anannas.ai', 443))"),
    ("getaddrinfo", "socket.create_connection(('api.anannas.ai', 443))"),
], ids=["dns", "connect", "connect-ex", "create-connection"])
def test_guard_refuses_vendor_before_dns_with_loopback_control(tmp_path, operation, expression):
    environment = child_environment(tmp_path)
    with FakeOpenAI() as fake:
        port = int(fake.base_url.split(":")[2].split("/")[0])
        code = (
            "import socket, sys\n"
            f"socket.create_connection(('127.0.0.1', {port}), timeout=5).close()\n"
            "print('loopback connection succeeded')\n"
            "try:\n"
            f"    {expression}\n"
            "except OSError as error:\n"
            "    print(str(error))\n"
            "    sys.exit(17)\n"
            "raise RuntimeError('guard did not refuse the vendor host')\n"
        )
        result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 17, result.stderr
    assert "loopback connection succeeded" in result.stdout
    assert f"loopback guard refused {operation}" in result.stdout
    assert_guard_ready(environment, blocked=operation)


def test_readme_requirements_and_customer_facing_pins():
    readme = (RECIPE / "README.md").read_text()
    for text in (
        "Anannas AI (OpenAI-compatible) with traceAI",
        "https://api.anannas.ai/v1",
        "https://anannas.ai/v1",
        "use `https://api.anannas.ai/v1`, the default here. Some older pages show `https://anannas.ai/v1`; the recipe accepts that host with the same URL rules.",
        "The provider field says `openai`",
        "Model IDs use the form `provider/model-name`",
        "gen_ai.request.model", "gen_ai.request.parameters",
        "openai/gpt-5-mini-2026-09-01", "assistant content", "no raw-response fallback",
        "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_PROJECT_NAME", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS",
        app.API_KEY_ENV, app.MODEL_ENV, app.BASE_URL_ENV,
        "https://docs.anannas.ai/", "https://langfuse.com/integrations/gateways/anannas",
        "pip install -r requirements.txt",
        "pip install traceAI-openai fi-instrumentation-otel openai",
        "python src/app.py", "python src/app.py --stream",
        "verbose=False", "openai==1.69.0", "not tested against the live provider (no paid call)",
    ):
        assert text in readme
    assert "TBD" not in readme
    assert "https://api.anannas.ai/v1/" in readme  # SDK appends the trailing slash.
    assert "APIStatusError" in readme and "AuthenticationError" in readme
    sections = ["## Install", "## Configure", "## Run", "## Code", "## What you see in Future AGI", "## Provider specifics", "## Privacy", "## Limits / not covered", "## Tests"]
    offsets = [readme.index(section) for section in sections]
    assert offsets == sorted(offsets)
    commands = re.findall(r"```bash\n(env -u PYTHONPATH.*?)\n```", readme, flags=re.S)
    assert len(commands) == 2
    common = (
        "env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \\\n"
        "  PYTHONPATH=\"{paths}\" \\\n"
        "  uv run --no-project --python 3.11 \\\n"
        "{package_lines}"
        "  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \\\n"
        "  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \\\n"
        "  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \\\n"
        "  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \\\n"
        "  pytest python/examples/anannas-ai/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs"
    )
    assert commands[0] == common.format(paths="python/examples/anannas-ai/src:python:python/frameworks/openai:python/tests", package_lines="")
    assert commands[1] == common.format(paths="python/examples/anannas-ai/src:python/tests", package_lines="  --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \\\n")
    pins = (RECIPE / "requirements.txt").read_text().splitlines()
    assert [line for line in pins if line and not line.startswith("#")] == ["openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0"]
    for file in (RECIPE / "README.md", RECIPE / "requirements.txt", RECIPE / "src" / "app.py", Path(__file__), RECIPE / "tests" / "_fake_openai.py", RECIPE / "tests" / "loopback_guard" / "sitecustomize.py"):
        contents = file.read_text()
        assert not re.search(r"\b[A-Z]{2,5}-\d{3,}\b", contents)
        assert not re.search(r"\b(?:sk|pk)[-_][A-Za-z0-9]{16,}\b", contents)
        assert not re.search(r"L[i]near|company[-]brain|prepared[ ]environment|R[i]ck", contents)
