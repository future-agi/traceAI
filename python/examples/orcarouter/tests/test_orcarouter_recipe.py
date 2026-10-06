"""Contract tests using MockTransport and guarded loopback subprocesses only."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import uuid

import app
import fi_instrumentation
import harness
import httpx
import openai
import pytest
import traceai_openai
from harness import Receiver
from traceai_openai import OpenAIInstrumentor

from _fake_openai import (
    ANSWER, REQUESTED_MODEL, RESOLVED_MODEL, USAGE,
    FakeOpenAI, completion, error_body, stream_bytes,
)

RECIPE = Path(__file__).resolve().parents[1]
VENDOR_KEY = "placeholder-orcarouter-key"
FI_KEY = "placeholder-futureagi-api-key"
FI_SECRET = "placeholder-futureagi-secret-key"
MESSAGES = [{"role": "user", "content": "What is a coral reef?"}]
MODEL_ATTRIBUTE = "gen_ai.request.model"
PARAMETERS_ATTRIBUTE = "gen_ai.request.parameters"
USAGE_ATTRIBUTES = {
    "gen_ai.usage.input_tokens": 13,
    "gen_ai.usage.output_tokens": 7,
    "gen_ai.usage.total_tokens": 20,
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # Clear ambient tracer settings so no real key or external endpoint is used.
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_", "ORCAROUTER_")) or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.MODEL_ENV, REQUESTED_MODEL)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    providers = []
    original_register = app.register

    def register(**kwargs):
        provider = original_register(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(app, "register", register)
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(host):
        if host not in ("127.0.0.1", "localhost", b"127.0.0.1", b"localhost"):
            raise AssertionError("Tests must never contact a non-loopback host")

    def getaddrinfo(host, *args, **kwargs):
        check(host)
        return original_dns(host, *args, **kwargs)

    def connect(sock, address):
        check(address[0] if isinstance(address, tuple) else None)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address[0] if isinstance(address, tuple) else None)
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    for provider in providers:
        provider.force_flush()
        provider.shutdown()


def attributes(span):
    result = {}
    for item in span["attributes"]:
        value = item["value"]
        for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if kind in value:
                result[item["key"]] = int(value[kind]) if kind == "intValue" else value[kind]
                break
    return result


def start_tracing(monkeypatch, receiver):
    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    project = "orcarouter-test-" + uuid.uuid4().hex
    return app.setup_tracing(project), project


def exported_span(receiver, provider, project):
    assert provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    span = spans[0]
    values = attributes(span)
    assert span["name"] == "ChatCompletion"
    assert values["gen_ai.span.kind"] == "LLM"
    assert values["gen_ai.provider.name"] == "openai"
    exports = receiver.requests()
    assert len(exports) == 1
    assert exports[0]["path"] == "/tracer/v1/traces"
    assert exports[0]["headers"]["x-api-key"] == FI_KEY
    assert exports[0]["headers"]["x-secret-key"] == FI_SECRET
    assert exports[0]["resource_attributes"][0]["project_name"] == project
    assert exports[0]["resource_attributes"][0]["project_type"] == "observe"
    assert VENDOR_KEY not in json.dumps({"spans": spans, "exports": exports})
    return span, values


def assert_vendor_request(request):
    assert str(request.url) == "https://api.orcarouter.ai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer " + VENDOR_KEY
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    assert FI_KEY not in str(request.headers)
    assert FI_SECRET not in str(request.headers)
    body = json.loads(request.content)
    assert body["model"] == REQUESTED_MODEL
    return body


def test_documented_default_and_client_overrides(monkeypatch):
    assert app.DEFAULT_BASE_URL == "https://api.orcarouter.ai/v1"
    assert "https://api.orcarouter.ai/v1" in (RECIPE / "README.md").read_text()
    with app.make_client() as client:
        assert str(client.base_url) == "https://api.orcarouter.ai/v1/"
        assert client.api_key == VENDOR_KEY
    monkeypatch.setenv(app.BASE_URL_ENV, "http://127.0.0.1:43210/custom")
    with app.make_client() as client:
        assert str(client.base_url) == "http://127.0.0.1:43210/custom/"
    with app.make_client(base_url="https://proxy.example.test/v1", api_key="placeholder-explicit-key") as client:
        assert str(client.base_url) == "https://proxy.example.test/v1/"
        assert client.api_key == "placeholder-explicit-key"
    monkeypatch.delenv(app.API_KEY_ENV)
    with pytest.raises(KeyError, match="ORCAROUTER_API_KEY"):
        app.make_client()


def test_chat_resolved_model_and_key_separation(monkeypatch):
    requests = []

    def handler(request):
        body = assert_vendor_request(request)
        assert body["messages"] == MESSAGES
        requests.append(request)
        return httpx.Response(200, json=completion())

    with Receiver() as receiver:
        provider, project = start_tracing(monkeypatch, receiver)
        with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            result = client.chat.completions.create(model=REQUESTED_MODEL, messages=MESSAGES)
        assert result.model == RESOLVED_MODEL
        assert result.choices[0].message.content == ANSWER
        span, values = exported_span(receiver, provider, project)
        assert span["status"]["code"] == "STATUS_CODE_OK"
        assert len(requests) == 1
        assert values[MODEL_ATTRIBUTE] == RESOLVED_MODEL
        assert json.loads(values[PARAMETERS_ATTRIBUTE])["model"] == REQUESTED_MODEL
        assert {key: values[key] for key in USAGE_ATTRIBUTES} == USAGE_ATTRIBUTES
        assert values["output.value"] == ANSWER


def test_usage_absent_is_omitted(monkeypatch):
    def handler(request):
        assert_vendor_request(request)
        return httpx.Response(200, json=completion(usage=False))

    with Receiver() as receiver:
        provider, project = start_tracing(monkeypatch, receiver)
        with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            result = client.chat.completions.create(model=REQUESTED_MODEL, messages=MESSAGES)
        assert result.usage is None
        _, values = exported_span(receiver, provider, project)
        assert values[MODEL_ATTRIBUTE] == RESOLVED_MODEL
        assert json.loads(values[PARAMETERS_ATTRIBUTE])["model"] == REQUESTED_MODEL
        assert not any(key.startswith("gen_ai.usage.") for key in values)


@pytest.mark.parametrize("include_usage", [False, True], ids=["default", "final-usage-chunk"])
def test_stream_text_model_gap_and_usage(monkeypatch, include_usage):
    requests = []

    def handler(request):
        body = assert_vendor_request(request)
        assert body["stream"] is True
        if include_usage:
            assert body["stream_options"] == {"include_usage": True}
        else:
            assert "stream_options" not in body
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=stream_bytes(include_usage=include_usage))

    with Receiver() as receiver:
        provider, project = start_tracing(monkeypatch, receiver)
        with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            options = {"stream_options": {"include_usage": True}} if include_usage else {}
            chunks = list(client.chat.completions.create(model=REQUESTED_MODEL, messages=MESSAGES, stream=True, **options))
        assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices) == ANSWER
        assert all(chunk.model == RESOLVED_MODEL for chunk in chunks)
        span, values = exported_span(receiver, provider, project)
        assert len(requests) == 1
        assert span["status"]["code"] == "STATUS_CODE_OK"
        assert values["output.value"] == ANSWER
        # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
        assert MODEL_ATTRIBUTE not in values
        assert json.loads(values[PARAMETERS_ATTRIBUTE])["model"] == REQUESTED_MODEL
        actual_usage = {key: value for key, value in values.items() if key.startswith("gen_ai.usage.")}
        assert actual_usage == (USAGE_ATTRIBUTES if include_usage else {})


def test_stream_without_text_has_no_raw_response_fallback(monkeypatch):
    marker = "unique-stream-metadata-" + uuid.uuid4().hex
    chunks = [json.loads(line[6:]) for line in stream_bytes(text="").decode().splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    for chunk in chunks:
        chunk["fixture_metadata"] = {"marker": marker}
    payload = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()

    def handler(request):
        assert assert_vendor_request(request)["stream"] is True
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=payload)

    with Receiver() as receiver:
        provider, project = start_tracing(monkeypatch, receiver)
        with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            received = list(client.chat.completions.create(model=REQUESTED_MODEL, messages=MESSAGES, stream=True))
        assert len(received) == 3
        assert all(chunk.fixture_metadata == {"marker": marker} for chunk in received)
        assert "".join(chunk.choices[0].delta.content or "" for chunk in received) == ""
        span, values = exported_span(receiver, provider, project)
        assert values["output.value"] == ""
        assert marker not in json.dumps(span)
        assert "fixture_metadata" not in json.dumps(span)
        # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
        assert MODEL_ATTRIBUTE not in values
        assert json.loads(values[PARAMETERS_ATTRIBUTE])["model"] == REQUESTED_MODEL


def test_fallback_chain_forwarded_but_absent_from_parameters(monkeypatch):
    models = ["a/one", "b/two"]
    bodies = []

    def handler(request):
        body = assert_vendor_request(request)
        assert body["models"] == models
        bodies.append(body)
        return httpx.Response(200, json=completion())

    with Receiver() as receiver:
        provider, project = start_tracing(monkeypatch, receiver)
        with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            client.chat.completions.create(model=REQUESTED_MODEL, messages=MESSAGES, extra_body={"models": models})
        _, values = exported_span(receiver, provider, project)
        assert len(bodies) == 1
        assert bodies[0]["models"] == models
        assert values[MODEL_ATTRIBUTE] == RESOLVED_MODEL
        parameters = json.loads(values[PARAMETERS_ATTRIBUTE])
        assert parameters["model"] == REQUESTED_MODEL
        assert "models" not in parameters
        assert "extra_body" not in parameters


@pytest.mark.parametrize("status,error_class", [(401, openai.AuthenticationError), (429, openai.RateLimitError)])
def test_errors_record_exception_and_requested_model(monkeypatch, status, error_class):
    requests = []

    def handler(request):
        assert_vendor_request(request)
        requests.append(request)
        return httpx.Response(status, json=error_body(status))

    with Receiver() as receiver:
        provider, project = start_tracing(monkeypatch, receiver)
        with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            client.max_retries = 0
            with pytest.raises(error_class) as caught:
                client.chat.completions.create(model=REQUESTED_MODEL, messages=MESSAGES)
        assert caught.value.status_code == status
        span, values = exported_span(receiver, provider, project)
        assert len(requests) == 1
        assert span["status"]["code"] == "STATUS_CODE_ERROR"
        exception_events = [event for event in span["events"] if event["name"] == "exception"]
        assert len(exception_events) == 1
        assert error_class.__name__ in json.dumps(exception_events)
        assert error_body(status)["error"]["message"] in json.dumps(exception_events)
        # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
        assert MODEL_ATTRIBUTE not in values
        assert json.loads(values[PARAMETERS_ATTRIBUTE])["model"] == REQUESTED_MODEL
        assert VENDOR_KEY not in json.dumps(span)


def test_hide_inputs_with_visible_control(monkeypatch):
    marker = "unique-prompt-marker-" + uuid.uuid4().hex
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hidden).lower())
        bodies = []

        def handler(request):
            bodies.append(assert_vendor_request(request))
            return httpx.Response(200, json=completion())

        with Receiver() as receiver:
            provider, project = start_tracing(monkeypatch, receiver)
            with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                client.chat.completions.create(model=REQUESTED_MODEL, messages=[{"role": "user", "content": marker}])
            span, values = exported_span(receiver, provider, project)
            assert bodies[0]["messages"][0]["content"] == marker
            assert (marker in json.dumps(span)) is (not hidden)
            assert ANSWER in values["output.value"]
            if hidden:
                assert values["input.value"] == "__REDACTED__"
                assert not any(key.startswith("gen_ai.input.messages") for key in values)
        OpenAIInstrumentor().uninstrument()


@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
def test_hide_outputs_with_visible_control(monkeypatch, stream):
    marker = "unique-answer-marker-" + uuid.uuid4().hex
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())

        def handler(request):
            assert_vendor_request(request)
            if stream:
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=stream_bytes(text=marker))
            return httpx.Response(200, json=completion(text=marker))

        with Receiver() as receiver:
            provider, project = start_tracing(monkeypatch, receiver)
            with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                result = client.chat.completions.create(model=REQUESTED_MODEL, messages=MESSAGES, stream=stream)
                if stream:
                    assert "".join(chunk.choices[0].delta.content or "" for chunk in result if chunk.choices) == marker
                else:
                    assert result.choices[0].message.content == marker
            span, values = exported_span(receiver, provider, project)
            assert (marker in json.dumps(span)) is (not hidden)
            if hidden:
                assert values["output.value"] == "__REDACTED__"
            assert MESSAGES[0]["content"] in json.dumps(span)
        OpenAIInstrumentor().uninstrument()


def test_extra_response_metadata_is_not_exported(monkeypatch):
    marker = "unique-response-metadata-" + uuid.uuid4().hex
    content_marker = "unique-assistant-content-" + uuid.uuid4().hex
    for hide_outputs in (False, True):
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hide_outputs).lower())

        def handler(request):
            assert_vendor_request(request)
            return httpx.Response(200, json=completion(
                text=content_marker, extra={"fixture_metadata": {"marker": marker}},
            ))

        with Receiver() as receiver:
            provider, project = start_tracing(monkeypatch, receiver)
            with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                result = client.chat.completions.create(model=REQUESTED_MODEL, messages=MESSAGES)
            # Positive controls: the SDK received both metadata and assistant content.
            assert result.fixture_metadata == {"marker": marker}
            assert result.choices[0].message.content == content_marker
            span, values = exported_span(receiver, provider, project)
            assert marker not in json.dumps(span)
            assert "fixture_metadata" not in json.dumps(span)
            assert (content_marker in json.dumps(span)) is (not hide_outputs)
            assert values["output.value"] == ("__REDACTED__" if hide_outputs else content_marker)
        OpenAIInstrumentor().uninstrument()


def child_environment(receiver, fake, tmp_path):
    environment = os.environ.copy()
    package_parents = [
        Path(fi_instrumentation.__file__).resolve().parent.parent,
        Path(traceai_openai.__file__).resolve().parent.parent,
        Path(harness.__file__).resolve().parent.parent,
    ]
    environment.update({
        "PYTHONPATH": os.pathsep.join(str(path) for path in [RECIPE / "tests/loopback_guard", RECIPE / "src", *package_parents]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FI_BASE_URL": receiver.origin,
        "FI_API_KEY": FI_KEY,
        "FI_SECRET_KEY": FI_SECRET,
        app.BASE_URL_ENV: fake.origin + "/v1",
        app.API_KEY_ENV: VENDOR_KEY,
        app.MODEL_ENV: REQUESTED_MODEL,
        "LOOPBACK_GUARD_LOG": str(tmp_path / "guard.log"),
        "LOOPBACK_GUARD_READY": str(tmp_path / "guard.ready"),
    })
    return environment


@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
def test_app_subprocess_with_installed_guard(monkeypatch, tmp_path, stream):
    with Receiver() as receiver, FakeOpenAI() as fake:
        environment = child_environment(receiver, fake, tmp_path)
        args = [sys.executable, str(RECIPE / "src/app.py")]
        if stream:
            args.append("--stream")
        result = subprocess.run(args, env=environment, capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ANSWER
        assert VENDOR_KEY not in result.stdout + result.stderr
        assert FI_KEY not in result.stdout + result.stderr
        assert FI_SECRET not in result.stdout + result.stderr
        assert (tmp_path / "guard.ready").read_text() == "installed\n"
        assert (tmp_path / "guard.log").read_text() == ""
        requests = fake.requests()
        assert len(requests) == 1
        assert requests[0]["path"] == "/v1/chat/completions"
        assert requests[0]["headers"]["authorization"] == "Bearer " + VENDOR_KEY
        assert "x-api-key" not in requests[0]["headers"]
        assert "x-secret-key" not in requests[0]["headers"]
        assert requests[0]["body"]["model"] == REQUESTED_MODEL
        assert requests[0]["body"]["stream"] is stream
        spans = receiver.spans()
        assert len(spans) == 1
        values = attributes(spans[0])
        assert spans[0]["name"] == "ChatCompletion"
        assert values["gen_ai.span.kind"] == "LLM"
        assert values["gen_ai.provider.name"] == "openai"
        assert ANSWER in values["output.value"]
        assert json.loads(values[PARAMETERS_ATTRIBUTE])["model"] == REQUESTED_MODEL
        if stream:
            # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
            assert MODEL_ATTRIBUTE not in values
            assert not any(key.startswith("gen_ai.usage.") for key in values)
        else:
            assert values[MODEL_ATTRIBUTE] == RESOLVED_MODEL
            assert {key: values[key] for key in USAGE_ATTRIBUTES} == USAGE_ATTRIBUTES
        exports = receiver.requests()
        assert len(exports) == 1
        assert exports[0]["path"] == "/tracer/v1/traces"
        assert exports[0]["headers"]["x-api-key"] == FI_KEY
        assert exports[0]["headers"]["x-secret-key"] == FI_SECRET
        assert exports[0]["resource_attributes"][0]["project_name"] == "orcarouter-example"
        assert VENDOR_KEY not in json.dumps({"spans": spans, "exports": exports})


def test_guard_refuses_dns_connect_and_connect_ex(tmp_path):
    script = """
import socket
checks = [lambda: socket.getaddrinfo('api.orcarouter.ai', 443)]
with socket.socket() as sock:
    checks.extend([lambda: sock.connect(('api.orcarouter.ai', 443)),
                   lambda: sock.connect_ex(('api.orcarouter.ai', 443))])
    for check in checks:
        try:
            check()
        except OSError as error:
            assert 'Loopback guard refused' in str(error)
        else:
            raise AssertionError('Guard did not refuse external access')
print('three operations refused')
"""
    with Receiver() as receiver, FakeOpenAI() as fake:
        result = subprocess.run([sys.executable, "-c", script], env=child_environment(receiver, fake, tmp_path), capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "three operations refused"
        assert (tmp_path / "guard.ready").read_text() == "installed\n"
        assert (tmp_path / "guard.log").read_text().splitlines() == ["Refused non-loopback host"] * 3
        assert fake.requests() == []
        assert receiver.spans() == []


def test_allowed_urls_are_returned_unchanged():
    for url in (
        "https://api.orcarouter.ai/v1", "https://api.orcarouter.ai/v1/",
        "https://API.ORCAROUTER.AI/v1", "https://api.orcarouter.ai./v1/",
        "http://127.0.0.1:43210/v1", "http://localhost:43210/v1/",
        "https://proxy.example.test/custom/v1",
    ):
        assert app.check_base_url(url) is url


def test_nul_url_is_refused_directly_before_parsing(monkeypatch):
    # Environment variables cannot contain NUL; exercise the validator directly.
    def forbidden_parse(*args, **kwargs):
        pytest.fail("Control characters must be refused before URL parsing")

    monkeypatch.setattr(app, "urlsplit", forbidden_parse)
    for url in ("\x00https://api.orcarouter.ai/v1", "https://api.orca\x00router.ai/v1", "https://api.orcarouter.ai/v1\x00"):
        with pytest.raises(ValueError) as caught:
            app.check_base_url(url)
        message = str(caught.value)
        assert app.BASE_URL_ENV in message
        assert "whitespace or control characters" in message
        assert "\x00" not in message
        assert "\n" not in message and "\r" not in message


def test_url_refusals_exit_before_tracing(monkeypatch, capsys):
    cases = [
        ("https://api.orcarouter.ai", "the root also serves other protocol surfaces"),
        ("https://api.orcarouter.ai/", "the root also serves other protocol surfaces"),
        ("https://API.ORCAROUTER.AI", "the root also serves other protocol surfaces"),
        ("https://api.orcarouter.ai./", "the root also serves other protocol surfaces"),
        ("https://api.orcarouter.ai/v1/chat/completions", "not an endpoint or another path"),
        ("https://API.ORCAROUTER.AI/v1/chat/completions", "not an endpoint or another path"),
        ("https://api.orcarouter.ai./v1/chat/completions", "not an endpoint or another path"),
        ("https://api.orcarouter.ai/v1/models", "not an endpoint or another path"),
        ("https://api.orcarouter.ai/anthropic", "not an endpoint or another path"),
        ("https://api.orcarouter.ai/gemini", "not an endpoint or another path"),
        ("https://api.orcarouter.ai/v2", "not an endpoint or another path"),
        ("https://api.orcarouter.ai/v1//", "not an endpoint or another path"),
        ("https://api.orcarouter.ai/%76%31", "not an endpoint or another path"),
        ("https://api.orcarouter.ai/v1/../v1", "not an endpoint or another path"),
        ("http://api.orcarouter.ai/v1", "HTTPS"),
        ("http://API.ORCAROUTER.AI./v1", "HTTPS"),
        ("https://api.orcarouter.ai/v1?", "query or fragment"),
        ("https://api.orcarouter.ai/v1?value=1", "query or fragment"),
        ("https://api.orcarouter.ai/v1#", "query or fragment"),
        ("https://api.orcarouter.ai/v1#section", "query or fragment"),
        ("https://API.ORCAROUTER.AI./v1?", "query or fragment"),
        ("https://placeholder-user:placeholder-password@api.orcarouter.ai/v1", "remove credentials"),
        ("http://placeholder-user@127.0.0.1:43210/v1", "remove credentials"),
        ("https://placeholder-user@proxy.example.test/v1", "remove credentials"),
        ("https://api.orcarouter.ai:bad/v1", "valid HTTP or HTTPS"),
        ("https://api.orcarouter.ai:65536/v1", "valid HTTP or HTTPS"),
        ("https://[api.orcarouter.ai/v1", "valid HTTP or HTTPS"),
        ("https:///v1", "with a host"),
        ("ftp://api.orcarouter.ai/v1", "HTTP or HTTPS"),
        ("https://xn--.example.test/v1", "valid IDNA"),
    ]
    for char in (" ", "\t", "\r", "\n", "\r\n", "\x1f", "\x7f", "\u00a0", "\u200b"):
        cases.append(("https://api.orca" + char + "router.ai/v1", "whitespace or control"))
    for char in ("\u3002", "\uff0e", "\uff61"):
        cases.append(("https://api" + char + "orcarouter.ai/v1", "plain ASCII"))

    def forbidden_setup(*args, **kwargs):
        pytest.fail("Invalid configuration must fail before setup_tracing")

    monkeypatch.setattr(app, "setup_tracing", forbidden_setup)
    with Receiver() as receiver, FakeOpenAI() as fake:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        for url, reason in cases:
            with pytest.raises(ValueError) as caught:
                app.check_base_url(url)
            message = str(caught.value)
            assert app.BASE_URL_ENV in message
            assert reason in message
            assert "\n" not in message and "\r" not in message
            assert "placeholder-user" not in message and "placeholder-password" not in message
            monkeypatch.setenv(app.BASE_URL_ENV, url)
            assert app.main([]) == 2
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == message + "\n"
            assert fake.requests() == []
            assert receiver.spans() == []
            assert receiver.requests() == []


@pytest.mark.parametrize("variable", ["ORCAROUTER_API_KEY", "ORCAROUTER_MODEL"])
@pytest.mark.parametrize("empty", [False, True], ids=["missing", "empty"])
def test_missing_values_exit_before_tracing(monkeypatch, capsys, variable, empty):
    def forbidden_setup(*args, **kwargs):
        pytest.fail("Missing configuration must fail before setup_tracing")

    monkeypatch.setattr(app, "setup_tracing", forbidden_setup)
    if empty:
        monkeypatch.setenv(variable, "")
    else:
        monkeypatch.delenv(variable)
    with Receiver() as receiver, FakeOpenAI() as fake:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv(app.BASE_URL_ENV, fake.origin + "/v1")
        assert app.main([]) == 2
        captured = capsys.readouterr()
        assert variable in captured.err
        assert captured.out == ""
        assert fake.requests() == []
        assert receiver.spans() == []
        assert receiver.requests() == []


def test_main_model_override_and_instrument_before_client(monkeypatch, capsys):
    monkeypatch.delenv(app.MODEL_ENV)
    with Receiver() as receiver, FakeOpenAI() as fake:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv(app.BASE_URL_ENV, fake.origin + "/v1")
        original_make_client = app.make_client

        def make_client(**kwargs):
            assert OpenAIInstrumentor().is_instrumented_by_opentelemetry
            return original_make_client(**kwargs)

        monkeypatch.setattr(app, "make_client", make_client)
        assert app.main(["--model", REQUESTED_MODEL, "--prompt", "A custom harmless prompt"]) == 0
        assert capsys.readouterr().out.strip() == ANSWER
        assert fake.requests()[0]["body"]["messages"] == [{"role": "user", "content": "A custom harmless prompt"}]
        spans = receiver.spans()
        assert len(spans) == 1
        assert attributes(spans[0])[MODEL_ATTRIBUTE] == RESOLVED_MODEL


def test_readme_contract_and_no_real_keys():
    readme = (RECIPE / "README.md").read_text()
    assert readme.startswith("# OrcaRouter (OpenAI-compatible) with traceAI")
    assert "https://api.orcarouter.ai/v1" in readme
    assert "provider field says `openai`" in readme
    for name in ("FI_API_KEY", "FI_SECRET_KEY", app.API_KEY_ENV, app.MODEL_ENV, app.BASE_URL_ENV, "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"):
        assert name in readme
    assert "gen_ai.request.model" in readme
    assert "gen_ai.request.parameters" in readme
    assert "cost lookup" in readme
    assert "models" in readme and "absent" in readme
    assert "https://docs.orcarouter.ai/introduction" in readme
    assert "verbose=False" in readme
    assert "TBD" not in readme
    source_command = """env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="python/examples/orcarouter/src:python:python/frameworks/openai:python/tests" \\
  uv run --no-project --python 3.11 \\
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \\
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \\
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \\
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \\
  pytest python/examples/orcarouter/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs"""
    assert source_command in readme
    assert 'PYTHONPATH="python/examples/orcarouter/src:python/tests"' in readme
    assert "--with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0'" in readme
    assert (RECIPE / "requirements.txt").read_text().splitlines()[1:] == [
        "openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0",
    ]
    forbidden_words = (
        "lin" + "ear", "ri" + "ck", "company-" + "brain",
        "prepared " + "environment", "private " + "links",
    )
    forbidden_pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, forbidden_words)) + r")\b", re.IGNORECASE)
    assert all(forbidden_pattern.search(word) for word in forbidden_words)
    for path in RECIPE.rglob("*"):
        if path.is_file():
            content = path.read_text()
            assert not re.search(r"\b[A-Z]{2,5}-\d{3,}\b", content), path
            assert not re.search(r"sk-[A-Za-z0-9_-]{16,}", content), path
            assert not forbidden_pattern.search(content), path
