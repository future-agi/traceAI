"""Perplexity Responses contracts using MockTransport and guarded loopback calls."""

import ast
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import fi_instrumentation
from harness import Receiver
import httpx
import openai
import pytest
import traceai_openai
from traceai_openai import OpenAIInstrumentor

TEST_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TEST_DIR.parent
VENDOR_KEY = "placeholder-perplexity-key"
FI_KEY = "placeholder-fi-key"
FI_SECRET = "placeholder-fi-secret"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = _load_module("perplexity_recipe_app", RECIPE_DIR / "src" / "app.py")
fake = _load_module("perplexity_recipe_fake", TEST_DIR / "_fake_openai.py")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OPENAI_", "PERPLEXITY_")) or name.lower() in (
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.MODEL_ENV, fake.REQUEST_MODEL)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


@pytest.fixture
def tracing(monkeypatch):
    @contextmanager
    def start():
        project = f"perplexity-fixture-{uuid.uuid4().hex}"
        with Receiver() as receiver:
            monkeypatch.setenv("FI_BASE_URL", receiver.origin)
            provider = app.setup_tracing(project_name=project)
            try:
                yield receiver, provider, project
            finally:
                provider.force_flush()
                OpenAIInstrumentor().uninstrument()
                provider.shutdown()
    return start


@pytest.fixture(scope="module")
def validation_servers():
    """All rejected configurations must leave both loopback servers untouched."""
    with Receiver() as receiver, fake.FakeOpenAI() as server:
        yield receiver, server


def _attributes(span):
    return {
        item["key"]: next(iter(item["value"].values()))
        for item in span.get("attributes", [])
    }


def _assert_export(receiver, provider, project, error=False, requested_model=fake.REQUEST_MODEL):
    assert provider.force_flush()
    [span] = receiver.spans()
    [export] = receiver.requests()
    attrs = _attributes(span)
    assert span["name"] == "Response"
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.provider.name"] == "openai"
    # A successful response overrides the request model, including on streams.
    assert attrs["gen_ai.request.model"] == (requested_model if error else fake.RESPONSE_MODEL)
    parameters = json.loads(attrs["gen_ai.request.parameters"])
    if requested_model is None:
        assert "model" not in parameters
    else:
        assert parameters["model"] == requested_model
    assert span["status"]["code"] == ("STATUS_CODE_ERROR" if error else "STATUS_CODE_OK")
    assert export["path"] == "/tracer/v1/traces"
    assert export["headers"]["x-api-key"] == FI_KEY
    assert export["headers"]["x-secret-key"] == FI_SECRET
    [resource] = export["resource_attributes"]
    assert resource["project_name"] == project
    assert resource["project_type"] == "observe"
    # Positive controls: a real span and collector authentication were observed.
    assert VENDOR_KEY not in json.dumps(span)
    assert VENDOR_KEY not in json.dumps(export)
    assert not any("citation" in key or "cost" in key for key in attrs)
    return span, attrs


def _assert_vendor_request(request, streamed=False, prompt=None, requested_model=fake.REQUEST_MODEL):
    assert str(request.url) == app.DEFAULT_BASE_URL + "/responses"
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {VENDOR_KEY}"
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    assert FI_KEY not in json.dumps(dict(request.headers))
    assert FI_SECRET not in json.dumps(dict(request.headers))
    body = json.loads(request.content)
    if requested_model is None:
        assert "model" not in body
    else:
        assert body["model"] == requested_model
    assert body.get("stream", False) is streamed
    if prompt is not None:
        assert body["input"] == prompt


def _assert_usage(attrs, present=True, details=True):
    usage = {key: int(value) for key, value in attrs.items() if key.startswith("gen_ai.usage.")}
    expected = {
        "gen_ai.usage.input_tokens": 17,
        "gen_ai.usage.output_tokens": 11,
        "gen_ai.usage.total_tokens": 28,
    } if present else {}
    if present and details:
        expected.update({
            "gen_ai.usage.output_tokens.reasoning": 4,
            "gen_ai.usage.input_tokens.cache_read": 9,
        })
    assert usage == expected


def _call(handler, stream=False, prompt="What is a solar eclipse?"):
    with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        if stream:
            events = list(client.responses.create(model=fake.REQUEST_MODEL, input=prompt, stream=True))
            assert [event.type for event in events] == [
                "response.created", *["response.output_text.delta"] * len(fake.DELTAS),
                "response.completed",
            ]
            assert events[-1].response.output_text == fake.ANSWER
            assert events[-1].response.output[0].type == "search_results"
            assert events[-1].response.model == fake.RESPONSE_MODEL
            assert fake.INSTRUCTIONS_MARKER in events[-1].response.instructions
            assert fake.QUERY_MARKER in json.dumps(events[-1].response.output[0].queries)
            return "".join(event.delta for event in events if event.type == "response.output_text.delta")
        response = client.responses.create(model=fake.REQUEST_MODEL, input=prompt)
        assert response.output[0].type == "search_results"
        assert response.output[1].type == "message"
        assert response.model == fake.RESPONSE_MODEL
        return response.output_text


def _handler(request, stream=False, include_usage=True, include_usage_details=True):
    _assert_vendor_request(request, streamed=stream)
    if stream:
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=fake.stream_fixture(
                                  include_usage=include_usage, include_usage_details=include_usage_details))
    return httpx.Response(200, json=fake.response_fixture(
        include_usage=include_usage, include_usage_details=include_usage_details))


def test_documented_base_url():
    assert app.DEFAULT_BASE_URL == "https://api.perplexity.ai/v1"
    assert app.DEFAULT_BASE_URL in (RECIPE_DIR / "README.md").read_text()
    with app.make_client() as client:
        assert str(client.base_url) == "https://api.perplexity.ai/v1/"


def test_make_client_overrides_and_missing_key(monkeypatch):
    monkeypatch.setenv(app.BASE_URL_ENV, "http://127.0.0.1:1234/v1")
    with app.make_client() as client:
        assert str(client.base_url) == "http://127.0.0.1:1234/v1/"
        assert client.api_key == VENDOR_KEY
    with app.make_client(base_url=app.DEFAULT_BASE_URL, api_key="placeholder-explicit-key") as client:
        assert str(client.base_url) == app.DEFAULT_BASE_URL + "/"
        assert client.api_key == "placeholder-explicit-key"
    monkeypatch.delenv(app.API_KEY_ENV)
    with pytest.raises(KeyError, match=app.API_KEY_ENV):
        app.make_client()


def test_responses_at_documented_host(tracing):
    assert fake.REQUEST_MODEL != fake.RESPONSE_MODEL
    with tracing() as (receiver, provider, project):
        with pytest.warns(UserWarning, match="search_results"):
            assert _call(_handler) == fake.ANSWER
        _, attrs = _assert_export(receiver, provider, project)
        _assert_usage(attrs)
        raw = ast.literal_eval(attrs["output.value"])
        assert raw["output"][0]["type"] == "search_results"
        assert raw["output"][0]["results"][0]["url"] == fake.SEARCH_URL
        assert raw["output"][0]["results"][0]["snippet"] == fake.SEARCH_SNIPPET
        assert raw["output"][0]["results"][0]["last_updated"] == "2026-10-02"
        assert raw["output"][0]["results"][1]["date"] is None
        assert fake.QUERY_MARKER in json.dumps(raw["output"][0]["queries"])
        assert raw["output"][1]["id"] == "msg_placeholder"
        assert raw["output"][1]["content"][0]["text"] == fake.ANSWER
        assert raw["usage"]["cost"] == fake.USAGE["cost"]
        # SDK serialization adds optional defaults such as cache_write_tokens=None.
        for key, value in fake.USAGE["input_tokens_details"].items():
            assert raw["usage"]["input_tokens_details"][key] == value
        assert raw["usage"]["output_tokens_details"] == fake.USAGE["output_tokens_details"]
        assert raw["usage"]["tool_calls_details"] == fake.USAGE["tool_calls_details"]
        assert fake.INSTRUCTIONS_MARKER in raw["instructions"]
        assert fake.SEARCH_URL in attrs["output.value"]
        assert fake.SEARCH_SNIPPET in attrs["output.value"]


def test_preset_fixture(tracing):
    prompt = "preset-fixture-input-marker"
    requests = []

    def handler(request):
        requests.append(request)
        _assert_vendor_request(request, prompt=prompt, requested_model=None)
        assert json.loads(request.content)["preset"] == "low"
        return httpx.Response(200, json=fake.response_fixture())

    with tracing() as (receiver, provider, project):
        with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            try:
                response = client.responses.create(input=prompt, extra_body={"preset": "low"})
            except TypeError as error:
                # Older SDKs (for example openai 1.69.0) require `model` on responses.create,
                # so a preset-only call fails in the SDK before any request or span.
                assert "model" in str(error)
                assert requests == []
                assert provider.force_flush()
                assert receiver.spans() == [] and receiver.requests() == []
                return
            assert response.output_text == fake.ANSWER
            assert response.model == fake.RESPONSE_MODEL
        assert len(requests) == 1
        _, attrs = _assert_export(receiver, provider, project, requested_model=None)
        _assert_usage(attrs)
        assert json.loads(attrs["gen_ai.request.parameters"]) == {}
        assert "preset" not in attrs["gen_ai.request.parameters"]


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
def test_usage_absent(tracing, stream):
    with tracing() as (receiver, provider, project):
        assert _call(lambda request: _handler(request, stream, include_usage=False), stream) == fake.ANSWER
        _, attrs = _assert_export(receiver, provider, project)
        _assert_usage(attrs, present=False)


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
def test_usage_without_details(tracing, stream):
    with tracing() as (receiver, provider, project):
        assert _call(lambda request: _handler(request, stream, include_usage_details=False), stream) == fake.ANSWER
        _, attrs = _assert_export(receiver, provider, project)
        _assert_usage(attrs, details=False)


def test_responses_stream(tracing):
    with tracing() as (receiver, provider, project):
        assert _call(lambda request: _handler(request, stream=True), stream=True) == fake.ANSWER
        _, attrs = _assert_export(receiver, provider, project)
        assert attrs["output.value"] == fake.ANSWER
        _assert_usage(attrs)
        # The non-streamed test proves these markers exist in the provider fixture.
        assert fake.SEARCH_URL not in json.dumps(attrs)
        assert fake.SEARCH_SNIPPET not in json.dumps(attrs)
        assert fake.QUERY_MARKER not in json.dumps(attrs)
        assert fake.INSTRUCTIONS_MARKER not in json.dumps(attrs)
        assert json.loads(attrs["gen_ai.request.parameters"])["stream"] is True


def test_authentication_error(tracing):
    def handler(request):
        _assert_vendor_request(request)
        return httpx.Response(401, json=fake.ERROR)

    with tracing() as (receiver, provider, project):
        with pytest.raises(openai.AuthenticationError, match="Invalid API key"):
            _call(handler)
        span, attrs = _assert_export(receiver, provider, project, error=True)
        assert "AuthenticationError" in span["status"]["message"]
        exceptions = [event for event in span["events"] if event["name"] == "exception"]
        assert len(exceptions) == 1
        assert "Invalid API key" in json.dumps(exceptions)
        _assert_usage(attrs, present=False)
        assert "output.value" not in attrs


def test_hide_inputs_with_control(tracing, monkeypatch):
    marker = "unique-perplexity-input-marker"
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hidden).lower())

        def handler(request):
            _assert_vendor_request(request, prompt=marker)
            return httpx.Response(200, json=fake.response_fixture())

        with tracing() as (receiver, provider, project):
            assert _call(handler, prompt=marker) == fake.ANSWER
            span, _ = _assert_export(receiver, provider, project)
            assert (marker in json.dumps(span)) is (not hidden)
            # Output echoes remain visible even when the input itself is hidden.
            for output_marker in (fake.QUERY_MARKER, fake.INSTRUCTIONS_MARKER):
                assert output_marker in _attributes(span)["output.value"]


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
def test_hide_outputs_with_control(tracing, monkeypatch, stream):
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())
        with tracing() as (receiver, provider, project):
            assert _call(lambda request: _handler(request, stream), stream) == fake.ANSWER
            span, attrs = _assert_export(receiver, provider, project)
            _assert_usage(attrs)
            assert (fake.ANSWER in json.dumps(span)) is (not hidden)
            for marker in (fake.SEARCH_URL, fake.SEARCH_SNIPPET, fake.QUERY_MARKER, fake.INSTRUCTIONS_MARKER):
                assert (marker in json.dumps(span)) is (not hidden and not stream)


def _child_environment(receiver, fake_server, tmp_path):
    environment = os.environ.copy()
    # These follow the actual imports, so published wheels work too.
    roots = [
        TEST_DIR / "loopback_guard", RECIPE_DIR / "src",
        Path(fi_instrumentation.__file__).resolve().parent.parent,
        Path(traceai_openai.__file__).resolve().parent.parent,
        Path(sys.modules[Receiver.__module__].__file__).resolve().parent.parent,
    ]
    environment.update({
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(str(path) for path in roots)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FI_BASE_URL": receiver.origin,
        "FI_API_KEY": FI_KEY,
        "FI_SECRET_KEY": FI_SECRET,
        app.API_KEY_ENV: VENDOR_KEY,
        app.BASE_URL_ENV: fake_server.base_url,
        app.MODEL_ENV: fake.REQUEST_MODEL,
        "LOOPBACK_GUARD_LOG": str(tmp_path / "guard.log"),
        "LOOPBACK_GUARD_READY": str(tmp_path / "guard.ready"),
        "NO_PROXY": "127.0.0.1,localhost",
    })
    return environment


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("cli_model", [False, True], ids=["env-model", "cli-model"])
def test_subprocess_app(tmp_path, stream, cli_model):
    prompt = "subprocess-perplexity-prompt-marker"
    with Receiver() as receiver, fake.FakeOpenAI() as server:
        environment = _child_environment(receiver, server, tmp_path)
        argv = [sys.executable, str(RECIPE_DIR / "src" / "app.py"), "--prompt", prompt]
        if stream:
            argv.append("--stream")
        if cli_model:
            environment.pop(app.MODEL_ENV)
            argv.extend(["--model", fake.REQUEST_MODEL])
        result = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == fake.ANSWER
        assert (tmp_path / "guard.ready").read_text() == "installed\n"
        assert (tmp_path / "guard.log").read_text() == ""
        [request] = server.requests()
        assert request["path"] == "/v1/responses"
        assert request["headers"]["authorization"] == f"Bearer {VENDOR_KEY}"
        assert "x-api-key" not in request["headers"]
        assert "x-secret-key" not in request["headers"]
        assert request["body"]["input"] == prompt
        assert request["body"]["model"] == fake.REQUEST_MODEL
        assert request["body"].get("stream", False) is stream
        [span] = receiver.spans()
        attrs = _attributes(span)
        assert span["name"] == "Response"
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.request.model"] == fake.RESPONSE_MODEL
        assert json.loads(attrs["gen_ai.request.parameters"])["model"] == fake.REQUEST_MODEL
        _assert_usage(attrs)
        assert fake.ANSWER in attrs["output.value"]
        assert (fake.SEARCH_URL in json.dumps(attrs)) is (not stream)
        assert (fake.SEARCH_SNIPPET in json.dumps(attrs)) is (not stream)
        assert (fake.QUERY_MARKER in json.dumps(attrs)) is (not stream)
        assert (fake.INSTRUCTIONS_MARKER in json.dumps(attrs)) is (not stream)
        [export] = receiver.requests()
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET
        assert export["resource_attributes"][0]["project_name"] == "perplexity-agent-api"
        assert VENDOR_KEY not in json.dumps(span)
        assert VENDOR_KEY not in json.dumps(export)


@pytest.mark.parametrize("operation", ["getaddrinfo", "connect", "connect_ex"])
def test_loopback_guard_refuses_external_hosts(tmp_path, operation):
    with Receiver() as receiver, fake.FakeOpenAI() as server:
        environment = _child_environment(receiver, server, tmp_path)
        script = """
import socket
import sys
import sitecustomize
assert socket.getaddrinfo('127.0.0.1', 80)
def underlying_call(*args, **kwargs):
    raise AssertionError('Underlying network operation was reached')
operation = sys.argv[1]
setattr(sitecustomize, '_' + operation, underlying_call)
try:
    if operation == 'getaddrinfo':
        socket.getaddrinfo('api.perplexity.ai', 443)
    else:
        with socket.socket() as connection:
            getattr(connection, operation)(('api.perplexity.ai', 443))
except RuntimeError as error:
    assert 'Loopback guard refused' in str(error)
    print('refused before networking')
else:
    raise AssertionError('Guard did not refuse the provider host')
"""
        result = subprocess.run([sys.executable, "-c", script, operation], env=environment,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "refused before networking"
        assert (tmp_path / "guard.ready").read_text() == "installed\n"
        assert (tmp_path / "guard.log").read_text() == f"{operation}: refused api.perplexity.ai\n"
        assert server.requests() == []
        assert receiver.spans() == []


REFUSED_PATHS = [
    ("", "Sonar Chat Completions"), ("/", "Sonar Chat Completions"),
    ("/v1/sonar", "Endpoint paths"), ("/v1/sonar/", "Endpoint paths"),
    ("/v1/agent", "Endpoint paths"), ("/v1/agent/", "Endpoint paths"),
    ("/v1/responses", "Endpoint paths"), ("/v1/responses/", "Endpoint paths"),
    ("/chat/completions", "Endpoint paths"), ("/chat/completions/", "Endpoint paths"),
    ("/v1/chat/completions", "Endpoint paths"), ("/v1/chat/completions/", "Endpoint paths"),
    ("/router/v1", "private-preview"), ("/router/v1/", "private-preview"),
    ("/router", "private-preview"), ("/router/", "private-preview"),
]


@pytest.mark.parametrize("host", ["api.perplexity.ai", "api.perplexity.ai.", "API.PERPLEXITY.AI"])
@pytest.mark.parametrize("path,reason", REFUSED_PATHS)
def test_check_base_url_refusals(host, path, reason):
    url = f"https://{host}{path}"
    with pytest.raises(ValueError, match=reason) as raised:
        app.check_base_url(url)
    message = str(raised.value)
    assert app.BASE_URL_ENV in message
    assert "\n" not in message
    assert "https://api.perplexity.ai/v1" in message
    assert "client.responses.create" in message
    if reason == "Sonar Chat Completions":
        assert "27 September 2026" in message
    assert url == f"https://{host}{path}"


@pytest.mark.parametrize("url", [
    "https://api.perplexity.ai/v1", "https://api.perplexity.ai/v1/",
    "https://API.PERPLEXITY.AI./v1/", "http://127.0.0.1:1234/v1",
    "http://localhost:1234/v1/",
])
def test_check_base_url_allowed(url):
    assert app.check_base_url(url) == url


@pytest.mark.parametrize("path,reason", REFUSED_PATHS)
@pytest.mark.parametrize("host", ["api.perplexity.ai", "api.perplexity.ai.", "API.PERPLEXITY.AI"])
def test_main_refuses_urls_before_tracing(monkeypatch, capsys, validation_servers, host, path, reason):
    _assert_main_refusal(f"https://{host}{path}", reason, monkeypatch, capsys, validation_servers)


def _assert_main_refusal(url, reason, monkeypatch, capsys, validation_servers):
    receiver, server = validation_servers
    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    monkeypatch.setenv(app.BASE_URL_ENV, url)
    monkeypatch.setattr(app, "setup_tracing", lambda *args, **kwargs: pytest.fail("Tracing started"))
    assert app.main([]) == 2
    captured = capsys.readouterr()
    assert reason in captured.err
    assert app.BASE_URL_ENV in captured.err
    assert captured.err.count("\n") == 1
    assert captured.out == ""
    assert "placeholder-user" not in captured.err
    assert "placeholder-password" not in captured.err
    assert server.requests() == []
    assert receiver.spans() == []
    assert receiver.requests() == []


@pytest.mark.parametrize("host", ["api.perplexity.ai", "api.perplexity.ai.", "API.PERPLEXITY.AI"])
@pytest.mark.parametrize("url_template,reason", [
    ("http://{host}/v1", "cleartext"),
    ("https://{host}/v1?fixture=query", "query or fragment"),
    ("https://{host}/v1?", "query or fragment"),
    ("https://{host}/v1#fixture", "query or fragment"),
    ("https://{host}/v1#", "query or fragment"),
    ("https://placeholder-user:placeholder-password@{host}/v1", "URL credentials"),
    ("https://placeholder-user@{host}/v1", "URL credentials"),
    ("https://@{host}/v1", "URL credentials"),
])
def test_base_url_security_refusals(monkeypatch, capsys, validation_servers, host, url_template, reason):
    url = url_template.format(host=host)
    with pytest.raises(ValueError, match=reason) as raised:
        app.check_base_url(url)
    assert app.BASE_URL_ENV in str(raised.value)
    assert "\n" not in str(raised.value)
    _assert_main_refusal(url, reason, monkeypatch, capsys, validation_servers)


@pytest.mark.parametrize("url", [
    "http://placeholder-user:placeholder-password@127.0.0.1:1234/v1",
    "http://placeholder-user@localhost:1234/v1",
    "https://placeholder-user:placeholder-password@example.invalid/v1",
])
def test_url_credentials_refused_on_any_host(monkeypatch, capsys, validation_servers, url):
    with pytest.raises(ValueError, match="URL credentials"):
        app.check_base_url(url)
    _assert_main_refusal(url, "URL credentials", monkeypatch, capsys, validation_servers)


@pytest.mark.parametrize("variable", [app.MODEL_ENV, app.API_KEY_ENV])
@pytest.mark.parametrize("empty", [False, True], ids=["missing", "empty"])
def test_main_requires_configuration_before_tracing(monkeypatch, capsys, variable, empty):
    with Receiver() as receiver, fake.FakeOpenAI() as server:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv(app.BASE_URL_ENV, server.base_url)
        if empty:
            monkeypatch.setenv(variable, "")
        else:
            monkeypatch.delenv(variable)
        monkeypatch.setattr(app, "setup_tracing", lambda *args, **kwargs: pytest.fail("Tracing started"))
        assert app.main([]) == 2
        captured = capsys.readouterr()
        assert variable in captured.err
        assert captured.out == ""
        assert server.requests() == []
        assert receiver.spans() == []
        assert receiver.requests() == []


def test_readme_pins():
    readme = (RECIPE_DIR / "README.md").read_text()
    assert readme.startswith("# Perplexity Agent API (OpenAI SDK, Responses) with traceAI\n")
    assert app.DEFAULT_BASE_URL in readme
    assert "provider field is `openai`" in readme
    assert "Sonar Chat Completions support ended on 27 September 2026" in readme
    assert "https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview" in readme
    assert "client.responses.create" in readme
    assert "gen_ai.request.model" in readme
    assert "on successful non-streamed and streamed calls records the model returned by the response" in readme
    assert "Failed calls record the model you sent" in readme
    assert "requested model remains in `gen_ai.request.parameters` on all three paths" in readme
    assert "Search results are not traced as citations or retriever spans; they appear only inside raw `output.value` on non-streamed calls." in readme
    assert "Search-result URLs and snippets appear inside `output.value` on non-streamed calls; streamed `output.value` contains only the answer text." in readme
    assert "gen_ai.usage.input_tokens.cache_read" in readme
    assert "gen_ai.usage.output_tokens.reasoning" in readme
    assert "when the response includes those details" in readme
    assert "The nested `usage.cost` object round-trips inside raw non-streamed output" in readme
    assert 'presets through the OpenAI SDK with `extra_body={"preset": ...}`' in readme
    assert "one span exported with the response model, while `preset` was absent from `gen_ai.request.parameters`" in readme
    assert '- Preset requests (`extra_body={"preset": ...}`).' in readme
    assert "With older SDKs such as `openai` 1.69.0, `responses.create` requires `model`" in readme
    assert "`FI_HIDE_INPUTS` does not remove them, so set `FI_HIDE_OUTPUTS=true` as well" in readme
    assert "Streamed fixture spans include neither the queries nor instructions" in readme
    assert "Synchronous and streaming requests keep working because they are being reformulated as Agent API requests, rolling out by model; asynchronous Sonar requests are no longer supported." in readme
    assert "set_global_tracer_provider=False, verbose=False" in readme
    for variable in (app.API_KEY_ENV, app.BASE_URL_ENV, app.MODEL_ENV, "FI_API_KEY",
                     "FI_SECRET_KEY", "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"):
        assert variable in readme
    assert "pip install -r requirements.txt" in readme
    assert "pip install traceAI-openai fi-instrumentation-otel openai" in readme
    assert "python src/app.py --stream" in readme
    command = """env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="python/examples/perplexity/src:python:python/frameworks/openai:python/tests" \\
  uv run --no-project --python 3.11 \\
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \\
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \\
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \\
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \\
  pytest python/examples/perplexity/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs"""
    assert command in readme
    assert 'PYTHONPATH="python/examples/perplexity/src:python/tests"' in readme
    assert "--with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0'" in readme
    table = """| Python | `openai` | traceAI packages | Result |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |"""
    assert table in readme
    assert "TBD" not in readme
    assert not re.search(r"\b(?:sk-|pplx-)[A-Za-z0-9_-]{12,}", readme)
    requirements = (RECIPE_DIR / "requirements.txt").read_text().splitlines()
    assert [line for line in requirements if line and not line.startswith("#")] == [
        "openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0",
    ]
