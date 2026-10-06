"""Fixture-only Azure v1 recipe contract; syntactic test hosts are not real hosts."""

import json
import os
import re
import socket
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import app
import fi_instrumentation
import httpx
import openai
import pytest
import traceai_openai
from harness import Receiver
from traceai_openai import OpenAIInstrumentor

from _fake_openai import (
    ANSWER, ERROR, FakeOpenAI, chat_response, chat_stream, responses_response,
)

RECIPE = Path(__file__).resolve().parents[1]
# Syntactic test hosts, not real Azure resources; MockTransport never resolves them.
TEST_BASE_URLS = (
    "https://test-resource-0000.openai.azure.com/openai/v1/",
    "https://test-resource-0000.services.ai.azure.com/openai/v1/",
)
MODEL = "my-gpt-deployment"
VENDOR_KEY = "placeholder-azure-key"
FI_KEY = "placeholder-futureagi-key"
FI_SECRET = "placeholder-futureagi-secret"
USAGE = {
    "gen_ai.usage.input_tokens": 7,
    "gen_ai.usage.output_tokens": 9,
    "gen_ai.usage.total_tokens": 16,
}
SOURCE_COMMAND = """env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="python/examples/azure-ai-foundry/src:python:python/frameworks/openai:python/tests" \\
  uv run --no-project --python 3.11 \\
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \\
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \\
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \\
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \\
  pytest python/examples/azure-ai-foundry/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs"""
PUBLISHED_COMMAND = """env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="python/examples/azure-ai-foundry/src:python/tests" \\
  uv run --no-project --python 3.11 \\
  --with 'openai==3.24.0' --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \\
  --with httpx --with 'wrapt<2' --with pytest \\
  pytest python/examples/azure-ai-foundry/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs"""


@pytest.fixture(autouse=True)
def isolate_environment_and_network(monkeypatch):
    # No inherited credentials, hiding flags, collector settings or proxy routes.
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_", "OPENAI_", "AZURE_")) or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.BASE_URL_ENV, TEST_BASE_URLS[0])
    monkeypatch.setenv(app.MODEL_ENV, MODEL)
    monkeypatch.setenv("NO_PROXY", "*")
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def require_loopback(host):
        if host not in ("127.0.0.1", "localhost", b"127.0.0.1", b"localhost"):
            raise RuntimeError("Tests refuse non-loopback networking before DNS")

    def guarded_dns(host, *args, **kwargs):
        require_loopback(host)
        return original_dns(host, *args, **kwargs)

    def guarded_connect(sock, address):
        require_loopback(address[0])
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        require_loopback(address[0])
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture
def receiver(monkeypatch):
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        yield receiver


@pytest.fixture
def tracing():
    @contextmanager
    def start():
        project = "azure-recipe-" + uuid.uuid4().hex
        provider = app.setup_tracing(project)
        try:
            yield provider, project
        finally:
            provider.force_flush()
            OpenAIInstrumentor().uninstrument()
            provider.shutdown()
    return start


def attributes(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def assert_usage(attrs, expected):
    actual = {key: int(value) for key, value in attrs.items() if key.startswith("gen_ai.usage.")}
    assert actual == expected


def assert_export(receiver, project, name="ChatCompletion"):
    spans, exports = receiver.spans(), receiver.requests()
    assert len(spans) == 1
    assert len(exports) == 1
    span = spans[0]
    attrs = attributes(span)
    assert span["name"] == name
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.provider.name"] == "openai"
    export = exports[0]
    assert export["path"] == "/tracer/v1/traces"
    assert "x-api-key" in export["headers"] and "x-secret-key" in export["headers"]
    assert export["headers"]["x-api-key"] == FI_KEY
    assert export["headers"]["x-secret-key"] == FI_SECRET
    assert export["resource_attributes"][0]["project_name"] == project
    assert export["resource_attributes"][0]["project_type"] == "observe"
    # The populated span/export and bearer assertion at each call site are controls.
    assert VENDOR_KEY not in json.dumps({"spans": spans, "exports": exports})
    return span, attrs


def assert_vendor_request(request, base_url, suffix, model=MODEL):
    assert str(request.url) == base_url + suffix
    assert request.headers["authorization"] == "Bearer " + VENDOR_KEY
    assert "x-api-key" not in request.headers and "x-secret-key" not in request.headers
    assert FI_KEY not in str(request.headers) and FI_SECRET not in str(request.headers)
    assert json.loads(request.content)["model"] == model


def mock_client(base_url, payload, requests, status=200):
    def handler(request):
        requests.append(request)
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(status, json=payload)
    return app.make_client(base_url=base_url, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def complete(client, prompt="What is a rainbow?", **kwargs):
    return client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}], **kwargs)


def test_documented_forms_require_resource_and_base_url(monkeypatch):
    assert app.DEFAULT_BASE_URL is None
    assert app.DOCUMENTED_BASE_URL_FORMS == (
        "https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1/",
        "https://YOUR-RESOURCE-NAME.services.ai.azure.com/openai/v1/",
    )
    readme = (RECIPE / "README.md").read_text()
    for form in app.DOCUMENTED_BASE_URL_FORMS:
        assert form in readme
        with pytest.raises(ValueError, match="placeholders"):
            app.check_base_url(form)
    monkeypatch.delenv(app.BASE_URL_ENV)
    with pytest.raises(ValueError, match=app.BASE_URL_ENV):
        app.make_client()


def test_sdk_appends_trailing_slash_without_validator_rewriting():
    supplied = TEST_BASE_URLS[0].rstrip("/")
    assert app.check_base_url(supplied) == supplied
    with app.make_client(base_url=supplied) as client:
        assert str(client.base_url) == TEST_BASE_URLS[0]


@pytest.mark.parametrize("base_url", TEST_BASE_URLS)
def test_chat_both_host_forms_and_key_separation(base_url, receiver, tracing, monkeypatch):
    monkeypatch.setenv(app.BASE_URL_ENV, base_url)
    requests = []
    with tracing() as (provider, project):
        with mock_client(base_url, chat_response(MODEL), requests) as client:
            assert str(client.base_url) == base_url
            assert complete(client).choices[0].message.content == ANSWER
        # The environment default is identical to the explicit client base URL.
        with app.make_client() as default_client:
            assert str(default_client.base_url) == base_url
        provider.force_flush()
        span, attrs = assert_export(receiver, project)
    assert len(requests) == 1
    assert_vendor_request(requests[0], base_url, "chat/completions")
    assert attrs["gen_ai.request.model"] == MODEL
    assert ANSWER in attrs["output.value"]
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert_usage(attrs, USAGE)


def test_usage_absent_is_omitted(receiver, tracing):
    requests = []
    with tracing() as (provider, project):
        with mock_client(TEST_BASE_URLS[0], chat_response(MODEL, usage=False), requests) as client:
            assert complete(client).choices[0].message.content == ANSWER
        provider.force_flush()
        _, attrs = assert_export(receiver, project)
    assert_vendor_request(requests[0], TEST_BASE_URLS[0], "chat/completions")
    assert attrs["gen_ai.request.model"] == MODEL
    assert_usage(attrs, {})
    # The same fixture's normal usage is verified in test_chat_both_host_forms_and_key_separation.


@pytest.mark.parametrize("include_usage", [False, True], ids=["default", "include-usage"])
def test_stream_output_model_gap_and_usage(include_usage, receiver, tracing):
    requests = []
    with tracing() as (provider, project):
        with mock_client(TEST_BASE_URLS[0], chat_stream(MODEL, include_usage), requests) as client:
            options = {"stream_options": {"include_usage": True}} if include_usage else {}
            text = "".join(chunk.choices[0].delta.content or "" for chunk in complete(client, stream=True, **options) if chunk.choices)
            assert text == ANSWER
        provider.force_flush()
        span, attrs = assert_export(receiver, project)
    assert_vendor_request(requests[0], TEST_BASE_URLS[0], "chat/completions")
    params = json.loads(attrs["gen_ai.request.parameters"])
    assert params["model"] == MODEL and params["stream"] is True
    assert json.loads(requests[0].content).get("stream_options") == ({"include_usage": True} if include_usage else None)
    # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
    assert "gen_ai.request.model" not in attrs
    assert attrs["output.value"] == ANSWER
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert_usage(attrs, USAGE if include_usage else {})


def test_authentication_error_records_exception_and_model_gap(receiver, tracing):
    requests = []
    with tracing() as (provider, project):
        with mock_client(TEST_BASE_URLS[0], ERROR, requests, status=401) as client:
            with pytest.raises(openai.AuthenticationError) as error:
                complete(client)
        provider.force_flush()
        span, attrs = assert_export(receiver, project)
    assert len(requests) == 1
    assert_vendor_request(requests[0], TEST_BASE_URLS[0], "chat/completions")
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert "invalid subscription key" in str(error.value)
    assert any(event["name"] == "exception" for event in span["events"])
    assert "AuthenticationError" in json.dumps(span["events"])
    assert "invalid subscription key" in json.dumps(span)
    # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
    assert "gen_ai.request.model" not in attrs
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == MODEL
    assert_usage(attrs, {})
    assert VENDOR_KEY not in str(error.value)


@pytest.mark.parametrize("flag,marker", [("FI_HIDE_INPUTS", "unique-private-prompt-marker"), ("FI_HIDE_OUTPUTS", ANSWER)])
def test_privacy_hiding_has_visible_control(flag, marker, receiver, tracing, monkeypatch):
    for hidden in (False, True):
        receiver.clear()
        monkeypatch.setenv(flag, str(hidden).lower())
        requests = []
        with tracing() as (provider, project):
            with mock_client(TEST_BASE_URLS[0], chat_response(MODEL), requests) as client:
                assert complete(client, prompt="unique-private-prompt-marker").choices[0].message.content == ANSWER
            provider.force_flush()
            assert_export(receiver, project)
        assert_vendor_request(requests[0], TEST_BASE_URLS[0], "chat/completions")
        assert json.loads(requests[0].content)["messages"][0]["content"] == "unique-private-prompt-marker"
        assert (marker in json.dumps(receiver.spans())) is (not hidden)


RETURNED_MODEL = "gpt-test-model-2026-09-01"


def test_responses_api_span_and_exact_url(receiver, tracing):
    requests = []
    with tracing() as (provider, project):
        # The response names a different (underlying) model than the deployment requested.
        with mock_client(TEST_BASE_URLS[0], responses_response(RETURNED_MODEL), requests) as client:
            response = client.responses.create(model=MODEL, input="What is a rainbow?")
            assert response.output_text == ANSWER
        provider.force_flush()
        span, attrs = assert_export(receiver, project, name="Response")
    assert len(requests) == 1
    assert_vendor_request(requests[0], TEST_BASE_URLS[0], "responses")
    assert requests[0].url.path == "/openai/v1/responses"
    assert json.loads(requests[0].content)["input"] == "What is a rainbow?"
    # On a successful Responses call the instrumentor records the model the response returns;
    # the requested deployment stays in the request parameters.
    assert attrs["gen_ai.request.model"] == RETURNED_MODEL
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == MODEL
    # Non-streamed Responses records the full parsed response's Python dict text.
    assert attrs["output.value"] == str(response.model_dump())
    assert response.model_dump()["output"][0]["content"][0]["text"] == ANSWER
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert_usage(attrs, {**USAGE, "gen_ai.usage.output_tokens.reasoning": 0, "gen_ai.usage.input_tokens.cache_read": 0})


WRONG_PATHS = (
    ("/api/projects/my-project", "project endpoint"),
    ("/openai/deployments/my-gpt-deployment", "AzureOpenAI"),
    ("/openai?api-version=2024-01-01", "AzureOpenAI"),
    ("/", "bare resource root"),
    ("", "bare resource root"),
    ("/models", "azure-ai-inference"),
)
AZURE_HOSTS = (
    "test-resource-0000.openai.azure.com",
    "TEST-RESOURCE-0000.SERVICES.AI.AZURE.COM",
    "test-resource-0000.cognitiveservices.azure.com.",
    "TEST-RESOURCE-0000.OPENAI.AZURE.COM.",
)


HARDENING_REFUSALS = (
    # The SDK folds the path into a query string, so the request would miss /chat/completions.
    ("https://test-resource-0000.openai.azure.com/openai/v1/?api-version=preview", "query string"),
    ("https://test-resource-0000.openai.azure.com/openai/v1?api-version=preview", "query string"),
    ("https://test-resource-0000.openai.azure.com/openai/v1/#frag", "query string"),
    # Encoded paths would pass a decoded check but go on the wire as a different path.
    ("https://test-resource-0000.openai.azure.com/openai%2Fv1/", "Percent-encoded paths"),
    ("https://test-resource-0000.openai.azure.com/openai/v1%3F/api/projects/x", "Percent-encoded paths"),
    # Plain http would send the Azure key in cleartext.
    ("http://test-resource-0000.openai.azure.com/openai/v1/", "must use https"),
    ("http://test-resource-0000.services.ai.azure.com/openai/v1/", "must use https"),
    # Credentials in the URL; this one only looks like Azure and would go to another host.
    ("https://test-resource-0000.openai.azure.com@proxy.example/openai/v1/", "credentials"),
    ("https://user:pass@test-resource-0000.openai.azure.com/openai/v1/", "credentials"),
)


@pytest.mark.parametrize("url,reason", HARDENING_REFUSALS)
def test_refuse_query_encoded_http_and_credentials(url, reason):
    with pytest.raises(ValueError, match=reason) as error:
        app.check_base_url(url)
    assert app.BASE_URL_ENV in str(error.value)
    assert "\n" not in str(error.value)


@pytest.mark.parametrize("host", AZURE_HOSTS)
@pytest.mark.parametrize("path,reason", WRONG_PATHS)
def test_refuse_wrong_azure_surfaces(host, path, reason):
    with pytest.raises(ValueError, match=reason) as error:
        app.check_base_url("https://" + host + path)
    assert app.BASE_URL_ENV in str(error.value)
    assert "\n" not in str(error.value)


@pytest.mark.parametrize("url", [
    *app.DOCUMENTED_BASE_URL_FORMS,
    "https://your-resource-name.OPENAI.AZURE.COM/openai/v1/",
    "https://<resource>.services.ai.azure.com/openai/v1/",
    "https://%3Cresource%3E.openai.azure.com/openai/v1/",
    "https://%253Cresource%253E.openai.azure.com/openai/v1/",
    "https://%59OUR-RESOURCE-NAME.openai.azure.com/openai/v1/",
    "https://%2559OUR-RESOURCE-NAME.openai.azure.com/openai/v1/",
    "https://test-resource-0000.services.ai.azure.com/api/projects/<project>",
    "https://test-resource-0000.openai.azure.com/openai/deployments/<name>",
])
def test_refuse_literal_encoded_and_double_encoded_placeholders(url):
    with pytest.raises(ValueError, match="Replace placeholders"):
        app.check_base_url(url)


@pytest.mark.parametrize("url", [
    *TEST_BASE_URLS,
    "https://test-resource-0000.openai.azure.com/openai/v1",
    "https://TEST-RESOURCE-0000.OPENAI.AZURE.COM./openai/v1/",
    "https://test-resource-0000.services.ai.azure.com./openai/v1/",
    "https://test-resource-0000.cognitiveservices.azure.com/openai/v1/",
    "https://proxy.example/custom/inference/",
    "https://proxy.example/models",
    "http://127.0.0.1:12345/openai/v1/",
    "http://localhost:12345/custom/",
])
def test_allowed_urls_are_returned_unchanged(url):
    assert app.check_base_url(url) == url


@pytest.mark.parametrize("missing", [app.BASE_URL_ENV, app.MODEL_ENV, app.API_KEY_ENV])
def test_missing_configuration_exits_before_tracing(missing, receiver, monkeypatch, capsys):
    monkeypatch.delenv(missing)
    monkeypatch.setattr(app, "setup_tracing", lambda *args, **kwargs: pytest.fail("Tracing ran before validation"))
    assert app.main([]) == 2
    assert missing in capsys.readouterr().err
    assert receiver.spans() == [] and receiver.requests() == []
    if missing == app.API_KEY_ENV:
        with pytest.raises(KeyError, match=app.API_KEY_ENV):
            app.make_client()


def test_refused_url_main_exits_without_requests_or_spans(receiver, monkeypatch, capsys):
    # The setup_tracing and make_client traps are the controls: no tracer, no client, no request.
    refused = "https://TEST-RESOURCE-0000.SERVICES.AI.AZURE.COM./api/projects/my-project"
    monkeypatch.setenv(app.BASE_URL_ENV, refused)
    monkeypatch.setattr(app, "setup_tracing", lambda *args, **kwargs: pytest.fail("Tracing ran for refused URL"))
    monkeypatch.setattr(app, "make_client", lambda *args, **kwargs: pytest.fail("Client created for refused URL"))
    assert app.main([]) == 2
    assert "project endpoint" in capsys.readouterr().err
    assert receiver.spans() == [] and receiver.requests() == []


def child_environment(tmp_path, receiver, base_url, project):
    # Resolve actual imported packages so this also works with published wheels.
    python_paths = [
        RECIPE / "tests" / "loopback_guard", RECIPE / "src",
        Path(fi_instrumentation.__file__).resolve().parent.parent,
        Path(traceai_openai.__file__).resolve().parent.parent,
        Path(sys.modules["harness"].__file__).resolve().parent.parent,
    ]
    return {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(str(path) for path in python_paths)),
        "NO_PROXY": "*",
        "FI_API_KEY": FI_KEY, "FI_SECRET_KEY": FI_SECRET,
        "FI_BASE_URL": receiver.origin, "FI_PROJECT_NAME": project,
        app.API_KEY_ENV: VENDOR_KEY, app.BASE_URL_ENV: base_url, app.MODEL_ENV: MODEL,
        "LOOPBACK_GUARD_READY": str(tmp_path / "guard-ready"),
        "LOOPBACK_GUARD_LOG": str(tmp_path / "guard.log"),
    }


def assert_child_guard(env):
    assert Path(env["LOOPBACK_GUARD_READY"]).read_text() == "loopback guard installed\n"
    assert Path(env["LOOPBACK_GUARD_LOG"]).read_text() == ""


@pytest.mark.parametrize("mode", ["chat", "stream", "model-override", "responses"])
def test_loopback_subprocess_journeys(mode, tmp_path, receiver):
    with FakeOpenAI() as fake:
        env = child_environment(tmp_path, receiver, fake.base_url, "azure-subprocess-" + uuid.uuid4().hex)
        model = MODEL
        if mode == "responses":
            project = env["FI_PROJECT_NAME"]
            command = [sys.executable, "-c", "import os, app; p = app.setup_tracing(os.environ['FI_PROJECT_NAME']); c = app.make_client(); print(c.responses.create(model=os.environ[app.MODEL_ENV], input='What is a rainbow?').output_text); c.close(); p.force_flush()"]
        else:
            project = "azure-ai-foundry"
            command = [sys.executable, str(RECIPE / "src" / "app.py")]
            if mode == "stream":
                command.append("--stream")
            elif mode == "model-override":
                model = "override-deployment"
                env.pop(app.MODEL_ENV)
                command.extend(["--model", model])
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ANSWER
        assert VENDOR_KEY not in result.stdout + result.stderr
        assert_child_guard(env)
        _, attrs = assert_export(receiver, project, name="Response" if mode == "responses" else "ChatCompletion")
        requests = fake.requests()
        assert len(requests) == 1
        assert requests[0]["path"] == ("/openai/v1/responses" if mode == "responses" else "/openai/v1/chat/completions")
        assert requests[0]["headers"]["authorization"] == "Bearer " + VENDOR_KEY
        assert "x-api-key" not in requests[0]["headers"] and "x-secret-key" not in requests[0]["headers"]
        assert requests[0]["body"]["model"] == model
        assert ANSWER in attrs["output.value"]
        if mode == "stream":
            # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
            assert "gen_ai.request.model" not in attrs
            assert json.loads(attrs["gen_ai.request.parameters"])["model"] == model
            assert_usage(attrs, {})
        else:
            assert attrs["gen_ai.request.model"] == model


def test_guard_refuses_before_dns_with_loopback_control(tmp_path, receiver):
    env = child_environment(tmp_path, receiver, TEST_BASE_URLS[0], "guard-control")
    host = "test-resource-0000.openai.azure.com"
    code = """import socket, sys, sitecustomize
from pathlib import Path
assert Path(sys.argv[1]).is_file()
assert socket.getaddrinfo('localhost', 80)
with socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=5):
    pass
def must_not_reach_network(*args, **kwargs):
    raise AssertionError('Non-loopback operation reached DNS or connect')
sitecustomize._getaddrinfo = must_not_reach_network
sitecustomize._connect = must_not_reach_network
sitecustomize._connect_ex = must_not_reach_network
for operation in (lambda: socket.getaddrinfo(sys.argv[3], 443),
                  lambda: socket.socket().connect((sys.argv[3], 443)),
                  lambda: socket.socket().connect_ex((sys.argv[3], 443))):
    try:
        operation()
    except RuntimeError as error:
        assert 'Loopback guard refused' in str(error)
    else:
        raise AssertionError('Guard permitted non-loopback host')
print('guard controls passed')
"""
    result = subprocess.run([sys.executable, "-c", code, env["LOOPBACK_GUARD_READY"], receiver.origin.rsplit(":", 1)[1], host], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "guard controls passed"
    log = Path(env["LOOPBACK_GUARD_LOG"]).read_text().splitlines()
    assert len(log) == 3 and all(host in line and "refused" in line for line in log)
    assert receiver.requests() == []


def test_readme_and_requirement_pins():
    readme = (RECIPE / "README.md").read_text()
    for value in (*app.DOCUMENTED_BASE_URL_FORMS, app.API_KEY_ENV, app.BASE_URL_ENV, app.MODEL_ENV, "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"):
        assert value in readme
    assert "model is your deployment name" in readme
    assert "provider field is `openai`" in readme
    assert "Not this page: Foundry Agent Service" in readme
    assert "agents are not this page" in readme
    assert "AzureAIOpenTelemetryTracer" in readme and "Application Insights" in readme
    assert SOURCE_COMMAND in readme and PUBLISHED_COMMAND in readme
    assert "https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle" in readme
    assert "syntactic test hosts" in readme and "not real" in readme
    assert "gen_ai.request.model" in readme and "TBD" not in readme
    assert "streamed and failed Chat Completions calls omit `gen_ai.request.model`" in readme
    assert "a successful Responses call records the model the response returns" in readme
    assert "may return the underlying model name rather than your deployment name" in readme
    assert "Requires Python 3.10 or later" in readme
    assert "Azure hosts must use `https://`" in readme
    assert "`FI_HIDE_INPUTS` does not cover" in readme
    assert "published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0`" in readme
    exports = dict(re.findall(r'^export (\w+)="([^"]*)"$', readme, re.MULTILINE))
    assert exports["FI_API_KEY"] == FI_KEY
    assert exports["FI_SECRET_KEY"] == FI_SECRET
    assert exports[app.API_KEY_ENV] == VENDOR_KEY
    assert not re.search(r"\b[A-Z]{2,5}-\d{3,}\b", readme), "internal ticket ids do not belong in the README"
    for forbidden in ("prepared environment", "sk-", "all models", "every call"):
        assert forbidden not in readme
    assert (RECIPE / "requirements.txt").read_text().splitlines()[1:] == [
        "openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0",
    ]
