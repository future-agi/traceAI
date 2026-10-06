"""Databricks recipe contracts: HTTP fixtures and loopback only."""

import atexit
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import uuid

import app
import fi_instrumentation
from harness import Receiver
import httpx
import openai
import pytest
import traceai_openai
from traceai_openai import OpenAIInstrumentor

from _fake_openai import (
    ANSWER, AUTH_ERROR, FakeOpenAI, chat_response, embedding_response, stream_response,
)

RECIPE = Path(__file__).resolve().parents[1]
GUARD = RECIPE / "tests" / "loopback_guard"
# Syntactic test hosts, not real ones. MockTransport never resolves these hosts.
GATEWAY = "https://dbc-00000000-0000.cloud.databricks.com/ai-gateway/mlflow/v1"
SERVING = "https://dbc-00000000-0000.cloud.databricks.com/serving-endpoints"
GATEWAY_MODEL = "system.ai.claude-sonnet-4-5"
ENDPOINT_MODEL = "openai-chat-endpoint"
VENDOR_KEY = "placeholder-databricks-key"
FI_KEY = "placeholder-fi-api-key"
FI_SECRET = "placeholder-fi-secret-key"
PROMPT = "unique-prompt-marker-databricks"
FORMS = (
    "https://<workspace-host>/ai-gateway/mlflow/v1",
    "https://<workspace-host>/serving-endpoints",
)


def attributes(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def only_span(receiver, provider=None, kind="LLM", name="ChatCompletion"):
    if provider is not None:
        assert provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    span = spans[0]
    assert span["name"] == name
    assert attributes(span)["gen_ai.span.kind"] == kind
    assert attributes(span)["gen_ai.provider.name"] == "openai"
    return span


def usage_attributes(span):
    return {key: int(value) for key, value in attributes(span).items() if key.startswith("gen_ai.usage.")}


def assert_usage(span):
    assert usage_attributes(span) == {
        "gen_ai.usage.input_tokens": 7,
        "gen_ai.usage.output_tokens": 11,
        "gen_ai.usage.total_tokens": 18,
    }


def assert_vendor_request(request, base, model):
    assert str(request.url) == base.rstrip("/") + "/chat/completions"
    assert request.headers["authorization"] == f"Bearer {VENDOR_KEY}"
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    assert FI_KEY not in str(request.headers)
    assert FI_SECRET not in str(request.headers)
    body = json.loads(request.content)
    assert body["model"] == model
    assert body["messages"][0]["content"] == PROMPT


def assert_export(receiver, project):
    exports = receiver.requests()
    assert len(exports) == 1
    export = exports[0]
    assert export["path"] == "/tracer/v1/traces"
    # Positive controls: the Future AGI keys reach the collector, and the project exists.
    assert export["headers"]["x-api-key"] == FI_KEY
    assert export["headers"]["x-secret-key"] == FI_SECRET
    assert export["resource_attributes"][0]["project_name"] == project
    assert export["resource_attributes"][0]["project_type"] == "observe"
    # The corresponding positive control is the vendor Authorization assertion.
    assert VENDOR_KEY not in json.dumps(receiver.spans())
    assert VENDOR_KEY not in json.dumps(exports)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_", "OPENAI_")) or name.lower() in (
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.BASE_URL_ENV, GATEWAY)
    monkeypatch.setenv(app.MODEL_ENV, GATEWAY_MODEL)

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check(host):
        if host not in ("127.0.0.1", "localhost"):
            raise OSError("Tests allow only loopback network connections")

    def connect(self, address):
        check(address[0] if isinstance(address, tuple) else address)
        return original_connect(self, address)

    def connect_ex(self, address):
        check(address[0] if isinstance(address, tuple) else address)
        return original_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        check(host)
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()


@pytest.fixture
def tracing(monkeypatch):
    providers = []
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)

        def start():
            project = "databricks-recipe-test-" + uuid.uuid4().hex
            provider = app.setup_tracing(project)
            providers.append(provider)
            return receiver, provider, project

        yield start
        if OpenAIInstrumentor().is_instrumented_by_opentelemetry:
            OpenAIInstrumentor().uninstrument()
        for provider in providers:
            provider.shutdown()
            atexit.unregister(provider.shutdown)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def complete(client, model=GATEWAY_MODEL, **kwargs):
    return client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": PROMPT}], **kwargs,
    )


def test_documented_forms_and_required_base_url(monkeypatch):
    assert app.DEFAULT_BASE_URL is None
    assert app.DOCUMENTED_BASE_URL_FORMS == FORMS
    readme = (RECIPE / "README.md").read_text()
    for form in FORMS:
        assert form in readme
        with pytest.raises(ValueError, match="placeholder"):
            app.check_base_url(form)
    monkeypatch.delenv(app.BASE_URL_ENV)
    with pytest.raises(ValueError, match=app.BASE_URL_ENV):
        app.make_client()


@pytest.mark.parametrize("base,model", [(GATEWAY, GATEWAY_MODEL), (SERVING, ENDPOINT_MODEL)])
def test_both_surfaces_chat_and_key_separation(base, model, monkeypatch, tracing):
    monkeypatch.setenv(app.BASE_URL_ENV, base)
    receiver, provider, project = tracing()
    requests = []

    def handler(request):
        requests.append(request)
        assert_vendor_request(request, base, model)
        return httpx.Response(200, json=chat_response(model))

    with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        # OpenAI adds a trailing slash; the recipe passes the original URL unchanged.
        assert str(client.base_url) == base + "/"
        assert complete(client, model).choices[0].message.content == ANSWER
    assert len(requests) == 1
    span = only_span(receiver, provider)
    assert attributes(span)["gen_ai.request.model"] == model
    assert attributes(span)["input.value"] == PROMPT
    assert attributes(span)["output.value"] == ANSWER
    assert_usage(span)
    assert_export(receiver, project)


def test_make_client_explicit_overrides_and_missing_key(monkeypatch):
    def handler(request):
        raise AssertionError("Creating a client must not make a request")

    with app.make_client(
        base_url=SERVING, api_key="placeholder-explicit-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ) as client:
        assert str(client.base_url) == SERVING + "/"
        assert client.api_key == "placeholder-explicit-key"
    monkeypatch.delenv(app.API_KEY_ENV)
    with pytest.raises(KeyError, match=app.API_KEY_ENV):
        app.make_client()


def test_usage_absent_is_omitted(tracing):
    receiver, provider, project = tracing()
    seen = []

    def handler(request):
        seen.append(request)
        assert_vendor_request(request, GATEWAY, GATEWAY_MODEL)
        return httpx.Response(200, json=chat_response(GATEWAY_MODEL, usage=False))

    with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        assert complete(client).choices[0].message.content == ANSWER
    span = only_span(receiver, provider)
    assert len(seen) == 1
    assert attributes(span)["gen_ai.request.model"] == GATEWAY_MODEL
    assert usage_attributes(span) == {}
    assert_export(receiver, project)
    # test_both_surfaces_chat_and_key_separation is the positive usage control.


@pytest.mark.parametrize("include_usage", [False, True], ids=["default", "final-usage-chunk"])
def test_stream_accumulation_and_usage(include_usage, tracing):
    receiver, provider, project = tracing()
    seen = []

    def handler(request):
        seen.append(request)
        assert_vendor_request(request, GATEWAY, GATEWAY_MODEL)
        body = json.loads(request.content)
        assert body["stream"] is True
        if include_usage:
            assert body["stream_options"] == {"include_usage": True}
        else:
            assert "stream_options" not in body
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=stream_response(GATEWAY_MODEL, include_usage))

    with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        options = {"stream_options": {"include_usage": True}} if include_usage else {}
        chunks = list(complete(client, stream=True, **options))
    assert len(seen) == 1
    assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices) == ANSWER
    span = only_span(receiver, provider)
    attrs = attributes(span)
    assert attrs["output.value"] == ANSWER
    # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
    assert "gen_ai.request.model" not in attrs
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == GATEWAY_MODEL
    if include_usage:
        assert_usage(span)
    else:
        assert usage_attributes(span) == {}
    assert_export(receiver, project)


def test_authentication_error_span(tracing):
    receiver, provider, project = tracing()
    seen = []

    def handler(request):
        seen.append(request)
        assert_vendor_request(request, GATEWAY, GATEWAY_MODEL)
        return httpx.Response(401, json=AUTH_ERROR)

    with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        with pytest.raises(openai.AuthenticationError) as error:
            complete(client)
    assert len(seen) == 1
    assert "Invalid access token." in str(error.value)
    span = only_span(receiver, provider)
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    events = [event for event in span["events"] if event["name"] == "exception"]
    assert events
    assert "AuthenticationError" in json.dumps(events)
    assert "Invalid access token." in json.dumps(span["status"])
    # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
    assert "gen_ai.request.model" not in attributes(span)
    assert json.loads(attributes(span)["gen_ai.request.parameters"])["model"] == GATEWAY_MODEL
    assert VENDOR_KEY not in str(error.value)
    assert_export(receiver, project)


def test_hide_inputs_and_outputs_have_visible_controls(monkeypatch, tracing):
    def handler(request):
        # Masking the export does not change the prompt sent to the provider.
        assert_vendor_request(request, GATEWAY, GATEWAY_MODEL)
        return httpx.Response(200, json=chat_response(GATEWAY_MODEL))

    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hidden).lower())
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())
        receiver, provider, project = tracing()
        with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            assert complete(client).choices[0].message.content == ANSWER
        span = only_span(receiver, provider)
        exported = json.dumps(span)
        assert (PROMPT in exported) is (not hidden)
        assert (ANSWER in exported) is (not hidden)
        assert_usage(span)
        assert_export(receiver, project)
        OpenAIInstrumentor().uninstrument()
        receiver.clear()


def test_embeddings_gateway_span(tracing):
    receiver, provider, project = tracing()
    model = "system.ai.gte-large-en"
    marker = "unique-embedding-input-databricks"
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == GATEWAY + "/embeddings"
        assert request.headers["authorization"] == f"Bearer {VENDOR_KEY}"
        assert "x-api-key" not in request.headers
        assert "x-secret-key" not in request.headers
        assert json.loads(request.content)["input"] == marker
        assert json.loads(request.content)["model"] == model
        return httpx.Response(200, json=embedding_response(model))

    with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        response = client.embeddings.create(model=model, input=marker)
        assert response.data[0].embedding == [0.125, 0.25, 0.5]
    assert len(seen) == 1
    span = only_span(receiver, provider, kind="EMBEDDING", name="CreateEmbeddingResponse")
    attrs = attributes(span)
    assert attrs["embedding.model_name"] == model
    assert "gen_ai.request.model" not in attrs
    assert json.loads(attrs["gen_ai.request.parameters"])["model"] == model
    assert usage_attributes(span) == {"gen_ai.usage.input_tokens": 7, "gen_ai.usage.total_tokens": 7}
    assert json.loads(attrs["input.value"]) == marker
    assert attrs["embedding.embeddings.0.embedding.text"] == marker
    assert marker in json.dumps(span)
    assert_export(receiver, project)


REFUSED = [
    ("https://<workspace-host>/ai-gateway/mlflow/v1", "placeholder"),
    ("https://<workspace-host>/serving-endpoints", "placeholder"),
    ("https://<workspace-name>.cloud.databricks.com/serving-endpoints", "placeholder"),
    ("https://%3Cworkspace-host%3E/serving-endpoints", "placeholder"),
    ("https://%253Cworkspace-host%253E/serving-endpoints", "placeholder"),
    (GATEWAY + "/%3Cname%3E", "placeholder"),
    (GATEWAY + "/%253Cname%253E", "placeholder"),
    ("https://example.staging.cloud.databricks.com/ai-gateway/mlflow/v1", "sample host"),
    ("https://EXAMPLE.STAGING.CLOUD.DATABRICKS.COM./serving-endpoints", "sample host"),
]
for host in (
    "dbc-00000000-0000.cloud.databricks.com",
    "DBC-00000000-0000.CLOUD.DATABRICKS.COM",
    "dbc-00000000-0000.cloud.databricks.com.",
    "adb-0000000000000000.0.azuredatabricks.net",
    "workspace-00000000.gcp.databricks.com",
):
    for path, reason in (
        ("", "Unsupported"), ("/", "Unsupported"),
        ("/serving-endpoints/<name>/invocations", "REST invocation"),
        ("/serving-endpoints/openai-chat-endpoint/invocations", "REST invocation"),
        ("/ai-gateway/gemini", "native APIs"),
        ("/ai-gateway/anthropic", "native APIs"),
        ("/api/2.0/serving-endpoints", "Unsupported"),
    ):
        REFUSED.append((f"https://{host}{path}", reason))


@pytest.mark.parametrize("url,reason", REFUSED)
def test_refused_urls_are_actionable_and_not_rewritten(url, reason):
    with pytest.raises(ValueError, match=reason) as error:
        app.check_base_url(url)
    message = str(error.value)
    assert "\n" not in message
    assert app.BASE_URL_ENV in message
    assert "/ai-gateway/mlflow/v1" in message
    assert "/serving-endpoints" in message
    # Allowed spellings below prove this is a selective check, rather than refusing all URLs.


@pytest.mark.parametrize("url", [
    GATEWAY, SERVING, GATEWAY + "/", SERVING + "/",
    GATEWAY.replace("dbc-00000000-0000.cloud.databricks.com", "DBC-00000000-0000.CLOUD.DATABRICKS.COM."),
    "https://adb-0000000000000000.0.azuredatabricks.net/serving-endpoints",
    "https://workspace-00000000.gcp.databricks.com/ai-gateway/mlflow/v1",
    "https://customer-proxy.example/custom/prefix/",
    "https://cloud.databricks.com.customer-proxy.example/custom/prefix",
    "http://127.0.0.1:12345/v1/", "http://localhost:12345/custom/",
])
def test_allowed_urls_are_returned_exactly(url):
    assert app.check_base_url(url) == url


@pytest.mark.parametrize("url,reason", REFUSED)
def test_main_refuses_before_tracing_or_network(url, reason, monkeypatch, capsys):
    def unexpected_tracing(*args, **kwargs):
        pytest.fail("setup_tracing ran before configuration validation")

    monkeypatch.setattr(app, "setup_tracing", unexpected_tracing)
    with Receiver() as receiver, FakeOpenAI() as fake:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv(app.BASE_URL_ENV, url)
        assert app.main([]) == 2
        output = capsys.readouterr()
        assert reason in output.err
        assert app.BASE_URL_ENV in output.err
        assert output.out == ""
        assert receiver.spans() == []
        assert receiver.requests() == []
        assert fake.requests() == []


@pytest.mark.parametrize("missing,empty", [
    (app.BASE_URL_ENV, False), (app.MODEL_ENV, False), (app.API_KEY_ENV, False),
    (app.BASE_URL_ENV, True), (app.MODEL_ENV, True), (app.API_KEY_ENV, True),
])
def test_main_missing_configuration_exits_before_tracing(missing, empty, monkeypatch, capsys):
    def unexpected_tracing(*args, **kwargs):
        pytest.fail("setup_tracing ran with missing configuration")

    monkeypatch.setattr(app, "setup_tracing", unexpected_tracing)
    if empty:
        monkeypatch.setenv(missing, "")
    else:
        monkeypatch.delenv(missing)
    assert app.main([]) == 2
    output = capsys.readouterr()
    assert missing in output.err
    assert output.out == ""
    assert VENDOR_KEY not in output.err


def child_environment(tmp_path, **overrides):
    child = os.environ.copy()
    # Use the packages actually imported, so the same tests work with published wheels.
    package_parents = [
        Path(fi_instrumentation.__file__).resolve().parent.parent,
        Path(traceai_openai.__file__).resolve().parent.parent,
    ]
    harness_parent = Path(sys.modules[Receiver.__module__].__file__).resolve().parent.parent
    paths = [GUARD, RECIPE / "src", harness_parent]
    paths.extend(package_parents)
    child.update({
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(str(path) for path in paths)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LOOPBACK_GUARD_LOG": str(tmp_path / "guard.log"),
        "LOOPBACK_GUARD_READY": str(tmp_path / "guard.ready"),
    })
    (tmp_path / "guard.log").write_text("")
    child.update(overrides)
    return child


@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
def test_app_subprocess_loopback(stream, tmp_path):
    with Receiver() as receiver, FakeOpenAI() as fake:
        child = child_environment(tmp_path, **{
            app.BASE_URL_ENV: fake.base_url, "FI_BASE_URL": receiver.origin,
        })
        command = [sys.executable, str(RECIPE / "src" / "app.py"), "--prompt", PROMPT]
        if stream:
            command.append("--stream")
        result = subprocess.run(command, env=child, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ANSWER
        assert (tmp_path / "guard.ready").read_text() == "LOOPBACK_GUARD_READY\n"
        assert (tmp_path / "guard.log").read_text() == ""
        seen = fake.requests()
        assert len(seen) == 1
        request = seen[0]
        assert request["path"] == "/v1/chat/completions"
        assert request["headers"]["authorization"] == f"Bearer {VENDOR_KEY}"
        assert "x-api-key" not in request["headers"]
        assert "x-secret-key" not in request["headers"]
        assert request["body"]["model"] == GATEWAY_MODEL
        assert request["body"]["messages"][0]["content"] == PROMPT
        assert request["body"]["stream"] is stream
        span = only_span(receiver)
        assert attributes(span)["output.value"] == ANSWER
        if stream:
            # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
            assert "gen_ai.request.model" not in attributes(span)
            assert json.loads(attributes(span)["gen_ai.request.parameters"])["model"] == GATEWAY_MODEL
            assert usage_attributes(span) == {}
        else:
            assert attributes(span)["gen_ai.request.model"] == GATEWAY_MODEL
            assert_usage(span)
        assert_export(receiver, "databricks-openai-recipe")
        for key in (VENDOR_KEY, FI_KEY, FI_SECRET):
            assert key not in result.stdout + result.stderr


def test_model_flag_overrides_environment(monkeypatch, tracing, capsys):
    receiver, provider, project = tracing()
    monkeypatch.setattr(app, "setup_tracing", lambda: provider)
    with FakeOpenAI() as fake:
        monkeypatch.setenv(app.BASE_URL_ENV, fake.base_url)
        monkeypatch.delenv(app.MODEL_ENV)
        assert app.main(["--model", ENDPOINT_MODEL, "--prompt", PROMPT]) == 0
        assert capsys.readouterr().out.strip() == ANSWER
        assert fake.requests()[0]["body"]["model"] == ENDPOINT_MODEL
    assert attributes(only_span(receiver, provider))["gen_ai.request.model"] == ENDPOINT_MODEL
    assert_export(receiver, project)


@pytest.mark.parametrize("operation", ["getaddrinfo", "connect", "connect_ex"])
def test_guard_refuses_before_dns_with_loopback_control(operation, tmp_path):
    host = "dbc-00000000-0000.cloud.databricks.com"  # Syntactic test host, not real.
    child = child_environment(tmp_path)
    with FakeOpenAI() as fake:
        port = int(fake.base_url.split(":")[2].split("/")[0])
        code = f"""
import socket
with socket.create_connection(('127.0.0.1', {port}), timeout=2):
    print('loopback-connected')
if {operation!r} == 'getaddrinfo':
    socket.getaddrinfo({host!r}, 443)
else:
    with socket.socket() as sock:
        getattr(sock, {operation!r})(({host!r}, 443))
"""
        result = subprocess.run([sys.executable, "-c", code], env=child, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "loopback-connected" in result.stdout
    assert "Loopback guard refused" in result.stderr
    assert (tmp_path / "guard.ready").read_text() == "LOOPBACK_GUARD_READY\n"
    assert (tmp_path / "guard.log").read_text() == f"REFUSED {host}\n"


SOURCE_TEST_COMMAND = """env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="python/examples/databricks/src:python:python/frameworks/openai:python/tests" \\
  uv run --no-project --python 3.11 \\
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \\
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \\
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \\
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \\
  pytest python/examples/databricks/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs"""


def test_readme_and_requirement_pins():
    readme = (RECIPE / "README.md").read_text()
    for form in FORMS:
        assert form in readme
    for name in (app.API_KEY_ENV, app.BASE_URL_ENV, app.MODEL_ENV, "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"):
        assert name in readme
    assert "The provider field says `openai`" in readme
    assert "example.staging.cloud.databricks.com is a placeholder, not a real host." in readme
    assert "`databricks_openai.DatabricksOpenAI` 0.17.1 subclasses `openai.OpenAI` without overriding its request method, so `traceai-openai` wraps it the same way; this recipe does not test it." in readme
    assert "A pricing row keyed by a foundation-model id will not match an endpoint name." in readme
    assert "Embeddings, only when the endpoint task is embeddings" in readme
    assert "gen_ai.request.model" in readme
    assert "trailing slash" in readme
    assert SOURCE_TEST_COMMAND in readme
    assert 'PYTHONPATH="python/examples/databricks/src:python/tests"' in readme
    assert "--with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0'" in readme
    assert "TBD" not in readme
    assert "| 3.10, 3.11, 3.12, 3.13 | 3.24.0 |" in readme
    assert "| 3.11 | 1.69.0 (the `traceai-openai` floor) |" in readme
    assert "published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0`" in readme
    for key_prefix in ("dapi-", "sk-"):
        assert key_prefix not in readme
    requirements = (RECIPE / "requirements.txt").read_text().splitlines()
    assert requirements[0].startswith("#")
    assert requirements[1:] == ["openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0"]
    assert "databricks-openai" not in "\n".join(requirements)
