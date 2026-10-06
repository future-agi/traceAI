"""Contract tests against synthetic responses and loopback-only receivers."""

from contextlib import contextmanager
import ast
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import uuid

import httpx
import openai
import pytest
import fi_instrumentation
import traceai_openai
from harness import Receiver
from traceai_openai import OpenAIInstrumentor

import app
from _fake_openai import (
    ANSWER, ERROR, REQUEST_MODEL, RESPONSE_MODEL, TOOL_CALL, TOOLS, USAGE,
    FakeOpenAI, chat_response, stream_body,
)

ROOT = Path(__file__).resolve().parents[1]
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
LOCAL_KEY = "not-needed"
EXPECTED_HTTP_WARNING = (
    "WARNING: PRISMML_BASE_URL uses HTTP on a non-loopback host; the Bonsai "
    "server is unauthenticated. Keep it on loopback or a trusted LAN."
)
USAGE_ATTRIBUTES = {
    "gen_ai.usage.input_tokens": 13,
    "gen_ai.usage.output_tokens": 7,
    "gen_ai.usage.total_tokens": 20,
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # Do not inherit real credentials, tracing settings or an external proxy.
    for name in list(os.environ):
        if name.startswith(("FI_", "OTEL_", "OPENAI_", "PRISMML_")) or name.lower() in (
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(host):
        assert host in ("127.0.0.1", "localhost"), "tests refuse non-loopback networking"

    def guarded_dns(host, *args, **kwargs):
        check(host)
        return original_dns(host, *args, **kwargs)

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()


@contextmanager
def tracing(monkeypatch):
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        project = "prismml-fixture-" + uuid.uuid4().hex
        provider = app.setup_tracing(project)
        try:
            yield receiver, provider, project
        finally:
            OpenAIInstrumentor().uninstrument()
            provider.force_flush()
            provider.shutdown()


@pytest.fixture
def traced(monkeypatch):
    with tracing(monkeypatch) as context:
        yield context


def attributes(span):
    return {item["key"]: next(iter(item["value"].values()))
            for item in span.get("attributes", [])}


def one_span(context):
    receiver, provider, _ = context
    assert provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    span = spans[0]
    assert span["name"] == "ChatCompletion"
    attrs = attributes(span)
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.provider.name"] == "openai"
    return span, attrs


def assert_usage(attrs, expected):
    actual = {key: int(value) for key, value in attrs.items() if key.startswith("gen_ai.usage.")}
    assert actual == expected


def assert_export_separation(receiver, project, key=LOCAL_KEY):
    requests = receiver.requests()
    assert len(requests) == 1
    export = requests[0]
    assert export["path"] == "/tracer/v1/traces"
    assert export["headers"]["x-api-key"] == FI_API_KEY
    assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
    assert export["resource_attributes"][0]["project_name"] == project
    assert export["resource_attributes"][0]["project_type"] == "observe"
    # The positive controls above and the bearer assertion at the server make
    # these key-absence assertions meaningful.
    assert key not in json.dumps(receiver.spans())
    assert key not in json.dumps(requests)


def mock_client(body=None, *, base_url=None, stream=False, status=200):
    seen = []

    def handler(request):
        seen.append(request)
        if stream:
            return httpx.Response(status, headers={"Content-Type": "text/event-stream"}, content=body)
        return httpx.Response(status, json=chat_response() if body is None else body)

    client = app.make_client(base_url=base_url,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return client, seen


def assert_local_request(request, url, key=LOCAL_KEY):
    assert str(request.url) == url
    assert request.headers["authorization"] == f"Bearer {key}"
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    assert FI_API_KEY not in json.dumps(dict(request.headers))
    assert FI_SECRET_KEY not in json.dumps(dict(request.headers))
    assert FI_API_KEY not in request.content.decode()
    assert FI_SECRET_KEY not in request.content.decode()
    assert json.loads(request.content)["model"] == REQUEST_MODEL


def test_documented_default_and_optional_key(monkeypatch):
    assert app.DEFAULT_BASE_URL == "http://localhost:8080/v1"
    assert app.API_KEY_ENV == "PRISMML_API_KEY"
    assert app.BASE_URL_ENV == "PRISMML_BASE_URL"
    assert app.MODEL_ENV == "PRISMML_MODEL"
    assert app.DEFAULT_BASE_URL in (ROOT / "README.md").read_text()
    with app.make_client() as client:
        assert str(client.base_url) == "http://localhost:8080/v1/"
        assert client.api_key == LOCAL_KEY
    monkeypatch.setenv(app.BASE_URL_ENV, "http://localhost:8081/v1")
    monkeypatch.setenv(app.API_KEY_ENV, "placeholder-prismml-key")
    with app.make_client() as client:
        assert str(client.base_url) == "http://localhost:8081/v1/"
        assert client.api_key == "placeholder-prismml-key"
    with app.make_client(base_url="http://127.0.0.1:8080/v1", api_key="explicit-placeholder") as client:
        assert str(client.base_url) == "http://127.0.0.1:8080/v1/"
        assert client.api_key == "explicit-placeholder"


@pytest.mark.parametrize("base_url", ["http://localhost:8080/v1", "http://localhost:8081/v1"])
@pytest.mark.parametrize("key", [LOCAL_KEY, "placeholder-prismml-key"])
def test_chat_model_usage_and_key_separation(traced, monkeypatch, base_url, key):
    monkeypatch.setenv(app.API_KEY_ENV, key)
    response = chat_response()
    response["fixture_extra"] = "raw-response-marker"
    client, seen = mock_client(response, base_url=base_url)
    with client:
        result = client.chat.completions.create(model=REQUEST_MODEL,
            messages=[{"role": "user", "content": "harmless fixture prompt"}])
    assert result.choices[0].message.content == ANSWER
    assert len(seen) == 1
    assert_local_request(seen[0], base_url + "/chat/completions", key)
    span, attrs = one_span(traced)
    assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
    assert RESPONSE_MODEL != REQUEST_MODEL
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == REQUEST_MODEL
    assert attrs["output.value"] == ANSWER
    assert "raw-response-marker" not in json.dumps(span)
    assert_usage(attrs, USAGE_ATTRIBUTES)
    assert_export_separation(traced[0], traced[2], key)


def test_usage_absent_is_omitted(traced):
    client, seen = mock_client(chat_response(include_usage=False))
    with client:
        client.chat.completions.create(model=REQUEST_MODEL,
            messages=[{"role": "user", "content": "usage fixture"}])
    assert len(seen) == 1
    _, attrs = one_span(traced)
    assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
    assert_usage(attrs, {})


@pytest.mark.parametrize("include_usage", [False, True])
def test_stream_accumulation_and_usage(traced, include_usage):
    client, seen = mock_client(stream_body(include_usage), stream=True)
    options = {"stream_options": {"include_usage": True}} if include_usage else {}
    with client:
        response = client.chat.completions.create(model=REQUEST_MODEL,
            messages=[{"role": "user", "content": "stream fixture"}], stream=True, **options)
        text = "".join(chunk.choices[0].delta.content or "" for chunk in response if chunk.choices)
    assert text == ANSWER
    assert len(seen) == 1
    assert_local_request(seen[0], "http://localhost:8080/v1/chat/completions")
    body = json.loads(seen[0].content)
    assert body["stream"] is True
    assert body.get("stream_options") == ({"include_usage": True} if include_usage else None)
    span, attrs = one_span(traced)
    assert attrs["output.value"] == ANSWER
    # traceai-openai records the model only from a non-streamed response; flip
    # this when the instrumentor records the request model.
    assert "gen_ai.request.model" not in attrs
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == REQUEST_MODEL
    assert_usage(attrs, USAGE_ATTRIBUTES if include_usage else {})
    assert any(event["name"] == "First Token Stream Event" for event in span["events"])
    assert_export_separation(traced[0], traced[2])


def test_textless_stream_has_no_raw_response_fallback(traced):
    client, seen = mock_client(stream_body(text=""), stream=True)
    with client:
        chunks = list(client.chat.completions.create(model=REQUEST_MODEL,
            messages=[{"role": "user", "content": "textless fixture"}], stream=True))
    assert chunks
    assert len(seen) == 1
    _, attrs = one_span(traced)
    assert attrs["output.value"] == ""
    assert "gen_ai.request.model" not in attrs
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == REQUEST_MODEL
    assert_usage(attrs, {})


def test_synthetic_401_records_error_without_key(traced):
    # The real server is unauthenticated; this is an SDK error-shape fixture.
    client, seen = mock_client(ERROR, status=401)
    with client, pytest.raises(openai.AuthenticationError):
        client.chat.completions.create(model=REQUEST_MODEL,
            messages=[{"role": "user", "content": "error fixture"}])
    assert len(seen) == 1
    assert_local_request(seen[0], "http://localhost:8080/v1/chat/completions")
    span, attrs = one_span(traced)
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    exceptions = [event for event in span["events"] if event["name"] == "exception"]
    assert len(exceptions) == 1
    assert "Synthetic fixture rejection" in json.dumps(exceptions)
    assert LOCAL_KEY not in json.dumps(span)
    # traceai-openai records the model only from a non-streamed response; flip
    # this when the instrumentor records the request model.
    assert "gen_ai.request.model" not in attrs
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == REQUEST_MODEL
    assert_usage(attrs, {})
    assert_export_separation(traced[0], traced[2])


@pytest.mark.parametrize("stream", [False, True])
def test_hide_inputs_with_control(monkeypatch, stream):
    marker = "private-prompt-marker-" + uuid.uuid4().hex
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hidden).lower())
        with tracing(monkeypatch) as context:
            client, seen = mock_client(stream_body() if stream else None, stream=stream)
            with client:
                result = client.chat.completions.create(model=REQUEST_MODEL,
                    messages=[{"role": "user", "content": marker}], stream=stream)
                if stream:
                    list(result)
            span, attrs = one_span(context)
            assert json.loads(seen[0].content)["messages"][0]["content"] == marker
            assert attrs["output.value"] == ANSWER
            if hidden:
                assert marker not in json.dumps(span)
                assert attrs["input.value"] == "__REDACTED__"
            else:
                assert marker in json.dumps(span)
                assert attrs["input.value"] == marker


@pytest.mark.parametrize("stream", [False, True])
def test_hide_outputs_with_control(monkeypatch, stream):
    marker = "private-output-marker-" + uuid.uuid4().hex
    prompt = "visible-prompt-marker"
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())
        response = chat_response()
        response["choices"][0]["message"]["content"] = marker
        with tracing(monkeypatch) as context:
            client, seen = mock_client(stream_body(text=marker) if stream else response, stream=stream)
            with client:
                result = client.chat.completions.create(model=REQUEST_MODEL,
                    messages=[{"role": "user", "content": prompt}], stream=stream)
                received = ("".join(chunk.choices[0].delta.content or "" for chunk in result)
                            if stream else result.choices[0].message.content)
            assert received == marker
            assert len(seen) == 1
            span, attrs = one_span(context)
            assert attrs["input.value"] == prompt
            if hidden:
                assert marker not in json.dumps(span)
                assert attrs["output.value"] == "__REDACTED__"
            else:
                assert marker in json.dumps(span)
                assert attrs["output.value"] == marker


def test_input_masking_does_not_mask_echoed_output(monkeypatch):
    marker = "echoed-prompt-marker"
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    response = chat_response()
    response["choices"][0]["message"]["content"] = marker
    with tracing(monkeypatch) as context:
        client, seen = mock_client(response)
        with client:
            client.chat.completions.create(model=REQUEST_MODEL,
                messages=[{"role": "user", "content": marker}])
        _, attrs = one_span(context)
        assert json.loads(seen[0].content)["messages"][0]["content"] == marker
        assert attrs["input.value"] == "__REDACTED__"
        assert attrs["output.value"] == marker
        assert not any(key.startswith("gen_ai.input.messages") for key in attrs)


def test_invocation_parameters_can_be_hidden_with_control(monkeypatch):
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_LLM_INVOCATION_PARAMETERS", str(hidden).lower())
        with tracing(monkeypatch) as context:
            client, seen = mock_client()
            with client:
                client.chat.completions.create(model=REQUEST_MODEL,
                    messages=[{"role": "user", "content": "parameter fixture"}])
            assert json.loads(seen[0].content)["model"] == REQUEST_MODEL
            _, attrs = one_span(context)
            assert attrs["output.value"] == ANSWER
            if hidden:
                assert "gen_ai.request.parameters" not in attrs
            else:
                assert json.loads(attrs["gen_ai.request.parameters"])["model"] == REQUEST_MODEL


@pytest.mark.parametrize("budget", [0, 12])
def test_thinking_extra_body_on_wire_but_absent_from_parameters(traced, budget):
    client, seen = mock_client()
    with client:
        client.chat.completions.create(model=REQUEST_MODEL,
            messages=[{"role": "user", "content": "thinking fixture"}],
            temperature=0.25, extra_body={"thinking_budget_tokens": budget})
    assert len(seen) == 1
    assert json.loads(seen[0].content)["thinking_budget_tokens"] == budget
    _, attrs = one_span(traced)
    params = json.loads(attrs["gen_ai.request.parameters"])
    assert params["model"] == REQUEST_MODEL
    assert params["temperature"] == 0.25
    # The SDK merges extra_body after the instrumentor reads json_data.
    assert "thinking_budget_tokens" not in params


def test_tool_call_records_definition_id_name_arguments_and_output(traced):
    client, seen = mock_client(chat_response(tool_call=True))
    with client:
        result = client.chat.completions.create(model=REQUEST_MODEL,
            messages=[{"role": "user", "content": "weather fixture"}], tools=TOOLS)
    assert len(seen) == 1
    assert json.loads(seen[0].content)["tools"] == TOOLS
    assert result.choices[0].message.tool_calls[0].id == TOOL_CALL["id"]
    _, attrs = one_span(traced)
    assert json.loads(attrs["gen_ai.tool.definitions"]) == TOOLS
    assert json.loads(attrs["gen_ai.tool.definitions.0.tool.json_schema"]) == TOOLS[0]
    prefix = "gen_ai.output.messages.0.message.tool_calls.0.tool_call."
    assert attrs[prefix + "id"] == TOOL_CALL["id"]
    assert attrs[prefix + "function.name"] == "get_weather"
    assert attrs[prefix + "function.arguments"] == '{"city":"Paris"}'
    assert attrs["output.value"] == 'Function: get_weather({"city":"Paris"})'
    assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == REQUEST_MODEL
    assert_usage(attrs, USAGE_ATTRIBUTES)


ALLOWED_URLS = [
    "http://localhost:8080/v1", "http://localhost:8081/v1/",
    "http://LOCALHOST.:8080/v1", "HTTP://localhost:8080/v1/",
    "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1/",
    "https://trusted-lan.example/v1", "http://trusted-lan.example:8080/v1",
    "http://TRUSTED-LAN.EXAMPLE.:8080/v1/",
]


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_allowed_base_urls_are_unchanged(url, capsys):
    result = app.check_base_url(url)
    assert result is url
    warning = "trusted-lan" in url.lower() and url.startswith("http:")
    assert capsys.readouterr().err == (EXPECTED_HTTP_WARNING + "\n" if warning else "")


REFUSED_URLS = [
    ("http://localhost:8080", "API path"),
    ("http://localhost:8080/", "API path"),
    ("http://LOCALHOST.:8080/", "API path"),
    ("http://localhost:8080/v1/chat/completions", "API path"),
    ("http://trusted-lan.example/v1/chat/completions", "API path"),
    ("https://trusted-lan.example/webchat", "API path"),
    ("http://localhost:8080/V1", "API path"),
    ("http://localhost:8080/v1//", "API path"),
    ("http://localhost:8080/v1/../v1", "API path"),
    ("http://localhost:8080/%76%31", "API path"),
    ("http://localhost:8080/v1%2f", "API path"),
    ("http://localhost:8080/v1?", "query or fragment"),
    ("http://localhost:8080/v1#", "query or fragment"),
    ("http://localhost:8080/v1?option=value", "query or fragment"),
    ("http://localhost:8080/v1#chat", "query or fragment"),
    ("https://trusted-lan.example/v1?", "query or fragment"),
    ("http://trusted-lan.example/v1#", "query or fragment"),
    ("http://credential-marker@localhost:8080/v1", "credentials"),
    ("https://credential-marker:password-marker@trusted-lan.example/v1", "credentials"),
    ("http://localhost:8080/\tv1", "whitespace"),
    ("http://localhost:8080/v1\r", "whitespace"),
    ("http://localhost:8080/v1\n", "whitespace"),
    (" http://localhost:8080/v1", "whitespace"),
    ("http://localhost:8080/v1 ", "whitespace"),
    ("http://local\x00host:8080/v1", "control"),
    ("http://localhost:8080/v1\x7f", "control"),
    ("http://local\u00a0host:8080/v1", "whitespace"),
    ("http://local\u200bhost:8080/v1", "control"),
    ("http://local\u3002host:8080/v1", "ASCII host"),
    ("http://local\uff0ehost:8080/v1", "ASCII host"),
    ("http://local\uff61host:8080/v1", "ASCII host"),
    ("http://caf\u00e9.example/v1", "ASCII host"),
    ("http://xn--.example/v1", "IDNA"),
    ("http://xn--a.example/v1", "IDNA"),
    ("http://local%68ost:8080/v1", "ASCII host"),
    ("http://local_host:8080/v1", "ASCII host"),
    ("http://-localhost:8080/v1", "ASCII host"),
    ("http://localhost..example/v1", "ASCII host"),
    ("http://localhost:invalid/v1", "port"),
    ("http://localhost:65536/v1", "port"),
    ("http://localhost:0/v1", "port"),
    ("http://localhost:/v1", "port"),
    ("http://[::1/v1", "host"),
    ("http://[::1%25zone]/v1", "ASCII host"),
    ("ftp://localhost:8080/v1", "http or https"),
    ("//localhost:8080/v1", "http or https"),
    ("http:///v1", "ASCII host"),
]


@pytest.fixture(scope="module")
def refusal_endpoints():
    with Receiver() as receiver, FakeOpenAI() as fake:
        yield receiver, fake


@pytest.mark.parametrize("url,reason", REFUSED_URLS)
def test_refused_base_urls_fail_before_tracing(url, reason, monkeypatch, capsys, refusal_endpoints):
    receiver, fake = refusal_endpoints
    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    if "\x00" in url:
        # OS environment variables cannot contain NUL; inject the mapping to
        # exercise the same main() validation path without an OS-level failure.
        monkeypatch.setattr(app.os, "environ", {app.BASE_URL_ENV: url})
    else:
        monkeypatch.setenv(app.BASE_URL_ENV, url)

    def must_not_run(*args, **kwargs):
        pytest.fail("invalid configuration reached tracing or client creation")

    monkeypatch.setattr(app, "setup_tracing", must_not_run)
    monkeypatch.setattr(app, "OpenAI", must_not_run)
    with pytest.raises(ValueError) as error:
        app.check_base_url(url)
    message = str(error.value)
    assert app.BASE_URL_ENV in message
    assert reason in message
    assert "\n" not in message and "\r" not in message
    assert "credential-marker" not in message and "password-marker" not in message
    assert app.main([]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == message + "\n"
    assert fake.requests() == []
    assert receiver.spans() == []
    assert receiver.requests() == []


def test_empty_explicit_key_is_actionable():
    with pytest.raises(ValueError, match="PRISMML_API_KEY"):
        app.make_client(api_key="")


def test_empty_cli_model_exits_before_tracing(monkeypatch, capsys, refusal_endpoints):
    receiver, fake = refusal_endpoints

    def must_not_run(*args, **kwargs):
        pytest.fail("empty model reached tracing")

    monkeypatch.setattr(app, "setup_tracing", must_not_run)
    assert app.main(["--model", ""]) == 2
    assert app.MODEL_ENV in capsys.readouterr().err
    assert fake.requests() == []
    assert receiver.spans() == []


def test_main_non_loopback_http_warns_once_and_instruments_before_client(traced, monkeypatch, capsys):
    receiver, provider, project = traced
    order, seen = [], []
    original_openai = app.OpenAI
    original_flush = provider.force_flush
    monkeypatch.setenv(app.BASE_URL_ENV, "http://trusted-lan.example:8080/v1")

    def setup():
        order.append("instrument")
        return provider

    def flush(*args, **kwargs):
        order.append("flush")
        return original_flush(*args, **kwargs)

    def client(**kwargs):
        order.append("client")
        assert order == ["instrument", "client"]

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=chat_response())

        return original_openai(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(app, "setup_tracing", setup)
    monkeypatch.setattr(app, "OpenAI", client)
    monkeypatch.setattr(provider, "force_flush", flush)
    assert app.main([]) == 0
    assert order == ["instrument", "client", "flush"]
    output = capsys.readouterr()
    assert output.err == EXPECTED_HTTP_WARNING + "\n"
    assert output.out == ANSWER + "\n"
    assert len(seen) == 1
    assert_local_request(seen[0], "http://trusted-lan.example:8080/v1/chat/completions")
    one_span(traced)
    assert_export_separation(receiver, project)


@pytest.mark.parametrize("cli_model", [None, "cli-model-fixture"])
def test_main_model_environment_and_cli_override(traced, monkeypatch, cli_model):
    expected = cli_model or "environment-model-fixture"
    monkeypatch.setenv(app.MODEL_ENV, "environment-model-fixture")
    monkeypatch.setattr(app, "setup_tracing", lambda: traced[1])
    original_openai = app.OpenAI
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=chat_response())

    def client(**kwargs):
        return original_openai(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(app, "OpenAI", client)
    assert app.main([] if cli_model is None else ["--model", cli_model]) == 0
    assert len(seen) == 1
    assert json.loads(seen[0].content)["model"] == expected
    _, attrs = one_span(traced)
    assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == expected


def subprocess_environment(tmp_path):
    # Resolve imported package roots so the same test works with published wheels.
    paths = [ROOT / "tests" / "loopback_guard", ROOT / "src",
             Path(fi_instrumentation.__file__).resolve().parent.parent,
             Path(traceai_openai.__file__).resolve().parent.parent,
             Path(sys.modules[Receiver.__module__].__file__).resolve().parent.parent]
    env = os.environ.copy()
    env.update(PYTHONPATH=os.pathsep.join(dict.fromkeys(map(str, paths))),
        PYTHONDONTWRITEBYTECODE="1", LOOPBACK_GUARD_LOG=str(tmp_path / "guard.log"),
        LOOPBACK_GUARD_READY=str(tmp_path / "guard.ready"))
    Path(env["LOOPBACK_GUARD_LOG"]).write_text("")
    return env


@pytest.mark.parametrize("stream", [False, True])
def test_app_subprocess_chat_and_stream(tmp_path, stream):
    with Receiver() as receiver, FakeOpenAI() as fake:
        env = subprocess_environment(tmp_path)
        env.update(FI_BASE_URL=receiver.origin, PRISMML_BASE_URL=fake.base_url,
                   PRISMML_MODEL=REQUEST_MODEL)
        if stream:
            env["PRISMML_API_KEY"] = "placeholder-prismml-key"
        # The non-streamed child exercises the default not-needed placeholder.
        argv = [sys.executable, str(ROOT / "src" / "app.py"), "--prompt", "subprocess fixture"]
        if stream:
            argv.append("--stream")
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert ANSWER in result.stdout
        assert FI_API_KEY not in result.stdout + result.stderr
        assert FI_SECRET_KEY not in result.stdout + result.stderr
        key = env.get("PRISMML_API_KEY", LOCAL_KEY)
        assert key not in result.stdout + result.stderr
        assert Path(env["LOOPBACK_GUARD_READY"]).read_text() == "installed\n"
        assert Path(env["LOOPBACK_GUARD_LOG"]).read_text() == ""
        requests = fake.requests()
        assert len(requests) == 1
        request = requests[0]
        assert request["path"] == "/v1/chat/completions"
        assert request["headers"]["authorization"] == f"Bearer {key}"
        assert "x-api-key" not in request["headers"]
        assert "x-secret-key" not in request["headers"]
        assert FI_API_KEY not in json.dumps(request)
        assert FI_SECRET_KEY not in json.dumps(request)
        assert request["body"]["model"] == REQUEST_MODEL
        assert request["body"]["stream"] is stream
        assert request["body"]["messages"][0]["content"] == "subprocess fixture"
        spans = receiver.spans()
        assert len(spans) == 1
        assert spans[0]["name"] == "ChatCompletion"
        attrs = attributes(spans[0])
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["output.value"] == ANSWER
        assert json.loads(attrs["gen_ai.request.parameters"])["model"] == REQUEST_MODEL
        if stream:
            assert "gen_ai.request.model" not in attrs
        else:
            assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
        assert_usage(attrs, {} if stream else USAGE_ATTRIBUTES)
        assert_export_separation(receiver, "prismml-bonsai", key)


@pytest.mark.parametrize("operation", ["getaddrinfo", "connect", "connect_ex"])
def test_guard_refuses_before_dns_or_connect_with_loopback_control(tmp_path, operation):
    env = subprocess_environment(tmp_path)
    with FakeOpenAI() as fake:
        port = int(fake.origin.rsplit(":", 1)[1])
        positive = subprocess.run([sys.executable, "-c",
            f"import socket; s=socket.create_connection(('127.0.0.1', {port})); s.close()"],
            env=env, capture_output=True, text=True, timeout=120)
        assert positive.returncode == 0, positive.stderr
    assert Path(env["LOOPBACK_GUARD_READY"]).read_text() == "installed\n"
    assert Path(env["LOOPBACK_GUARD_LOG"]).read_text() == ""
    # This local-server recipe has no remote provider API host. Use the public
    # documentation host only as a guard target; it must never reach DNS.
    expression = ("socket.getaddrinfo('docs.prismml.com', 443)" if operation == "getaddrinfo"
                  else f"socket.socket().{operation}(('docs.prismml.com', 443))")
    negative = subprocess.run([sys.executable, "-c", "import socket; " + expression],
        env=env, capture_output=True, text=True, timeout=120)
    assert negative.returncode != 0
    assert "loopback guard refused" in negative.stderr
    assert Path(env["LOOPBACK_GUARD_LOG"]).read_text() == "BLOCKED non-loopback socket operation\n"


def test_readme_and_requirements_contract():
    readme = (ROOT / "README.md").read_text()
    assert readme.startswith("# PrismML (OpenAI-compatible) with traceAI\n")
    for phrase in ("http://localhost:8080/v1", "http://localhost:8081/v1",
                   "provider field says `openai`", "gen_ai.request.model", "verbose=False",
                   "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS",
                   "thinking_budget_tokens", "tool_calls", "not-needed", "cost"):
        assert phrase in readme
    source = (ROOT / "src" / "app.py").read_text()
    for name in (app.API_KEY_ENV, app.BASE_URL_ENV, app.MODEL_ENV):
        assert name in readme
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if ast.unparse(node.func).startswith("os.environ.") and node.args:
                if isinstance(node.args[0], ast.Constant):
                    assert node.args[0].value in readme
    headings = ["## Install", "## Configure", "## Run", "## Code",
                "## What you see in Future AGI", "## Provider specifics", "## Privacy",
                "## Limits / not covered", "## Tests"]
    assert [readme.index(heading) for heading in headings] == sorted(readme.index(heading) for heading in headings)
    for paths in ("python/examples/prismml/src:python:python/frameworks/openai:python/tests",
                  "python/examples/prismml/src:python/tests"):
        assert f'PYTHONPATH="{paths}"' in readme
    assert readme.count("pytest python/examples/prismml/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs") == 2
    assert "--with 'traceAI-openai==0.1.10'" in readme
    assert "--with 'fi-instrumentation-otel==1.1.0'" in readme
    assert "TBD" not in readme
    assert "not tested against the live provider" in readme
    requirements = (ROOT / "requirements.txt").read_text().splitlines()
    assert [line for line in requirements if not line.startswith("#")] == [
        "openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0"]
    for path in ROOT.rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert not re.search(r"\b[A-Z]{2,5}-\d{3,}\b", text), str(path)
            assert not re.search(r"\bsk-[A-Za-z0-9]{16,}\b", text), str(path)
    assert not re.search(r"\b(?:Rick|Linear|brief)\b|company-brain", readme)
