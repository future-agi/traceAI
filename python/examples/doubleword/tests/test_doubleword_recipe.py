"""Offline contracts for the recipe, real instrumentor, SDK, and OTLP exporter."""

import inspect
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from uuid import uuid4

import app
import fi_instrumentation
import harness
from harness import Receiver
import httpx
import openai
import pytest
import traceai_openai
from traceai_openai import OpenAIInstrumentor

from _fake_openai import (
    ANSWER, MODEL, RETURNED_MODEL, VECTOR, FakeOpenAI,
    completion, embeddings, error_body, stream_bytes,
)

RECIPE = Path(__file__).resolve().parents[1]
VENDOR_KEY = "placeholder-doubleword-key"
FI_KEY = "placeholder-futureagi-api-key"
FI_SECRET = "placeholder-futureagi-secret-key"
SDK_VERSION = tuple(int(part) for part in openai.__version__.split(".")[:3])
USAGE_ATTRIBUTES = {
    "gen_ai.usage.input_tokens": 13,
    "gen_ai.usage.output_tokens": 7,
    "gen_ai.usage.total_tokens": 20,
}


def attributes(span):
    result = {}
    for item in span.get("attributes", []):
        value = item["value"]
        if "arrayValue" in value:
            result[item["key"]] = [next(iter(v.values())) for v in value["arrayValue"]["values"]]
        else:
            result[item["key"]] = next(iter(value.values()))
    return result


def usage_attributes(attrs):
    return {k: int(v) for k, v in attrs.items() if k.startswith("gen_ai.usage.")}


def only_span(receiver, provider):
    assert provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    return spans[0], attributes(spans[0])


def assert_key_separation(request, receiver):
    # Positive control: the vendor key was actually used on the vendor request.
    assert request.headers["authorization"] == f"Bearer {VENDOR_KEY}"
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    assert FI_KEY not in str(request.headers)
    assert FI_SECRET not in str(request.headers)
    exports = receiver.requests()
    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET
    assert VENDOR_KEY not in json.dumps(receiver.spans())
    assert VENDOR_KEY not in json.dumps(exports)


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("FI_"):
            monkeypatch.delenv(name)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.MODEL_ENV, MODEL)
    monkeypatch.delenv(app.BASE_URL_ENV, raising=False)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check(host):
        if host not in ("127.0.0.1", "localhost", b"127.0.0.1", b"localhost"):
            raise OSError("test refuses non-loopback network access")

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
    yield
    if OpenAIInstrumentor().is_instrumented_by_opentelemetry:
        OpenAIInstrumentor().uninstrument()


@pytest.fixture
def journey(monkeypatch):
    @contextmanager
    def start():
        with Receiver() as receiver:
            monkeypatch.setenv("FI_BASE_URL", receiver.origin)
            project = f"doubleword-test-{uuid4().hex}"
            provider = app.setup_tracing(project_name=project)
            try:
                yield receiver, provider, project
            finally:
                provider.force_flush()
                OpenAIInstrumentor().uninstrument()
                provider.shutdown()
    return start


@contextmanager
def mock_client(handler):
    with app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        # Tests disable SDK retries so one fixture response means one request;
        # the recipe itself keeps the SDK's default retry policy.
        yield client.with_options(max_retries=0)


def test_documented_base_url_and_sdk_trailing_slash():
    assert app.DEFAULT_BASE_URL == "https://api.doubleword.ai/v1"
    assert app.DEFAULT_BASE_URL in (RECIPE / "README.md").read_text()
    with mock_client(lambda request: pytest.fail("client construction must not send a request")) as client:
        assert str(client.base_url) == "https://api.doubleword.ai/v1/"
        assert client.api_key == VENDOR_KEY


def test_realtime_chat_response_model_usage_and_key_separation(journey):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    with journey() as (receiver, provider, project), mock_client(handler) as client:
        response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Chat control marker"}])
        assert response.choices[0].message.content == ANSWER
        span, attrs = only_span(receiver, provider)
        assert span["name"] == "ChatCompletion"
        assert span["status"]["code"] == "STATUS_CODE_OK"
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.request.model"] == RETURNED_MODEL
        assert RETURNED_MODEL != MODEL
        assert json.loads(attrs["gen_ai.request.parameters"])["model"] == MODEL
        assert usage_attributes(attrs) == USAGE_ATTRIBUTES
        assert ANSWER in attrs["output.value"]
        assert "Chat control marker" in json.dumps(span)
        assert len(requests) == 1
        assert str(requests[0].url) == "https://api.doubleword.ai/v1/chat/completions"
        assert json.loads(requests[0].content)["model"] == MODEL
        assert_key_separation(requests[0], receiver)
        assert receiver.requests()[0]["resource_attributes"][0]["project_name"] == project


def test_usage_absent_is_omitted(journey):
    with journey() as (receiver, provider, _), mock_client(lambda r: httpx.Response(200, json=completion(usage=False))) as client:
        client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Usage control"}])
        _, attrs = only_span(receiver, provider)
        assert attrs["gen_ai.request.model"] == RETURNED_MODEL
        assert ANSWER in attrs["output.value"]
        assert usage_attributes(attrs) == {}


@pytest.mark.parametrize("include_usage", [False, True])
def test_stream_text_model_gap_and_usage(journey, include_usage):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream_bytes(include_usage=include_usage))

    with journey() as (receiver, provider, _), mock_client(handler) as client:
        options = {"stream_options": {"include_usage": True}} if include_usage else {}
        stream = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Stream control"}], stream=True, **options)
        with stream:
            text = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
        assert text == ANSWER
        span, attrs = only_span(receiver, provider)
        assert span["name"] == "ChatCompletion"
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["output.value"] == ANSWER
        # traceai-openai records the model only from a non-streamed response;
        # flip this when the instrumentor records the request model.
        assert "gen_ai.request.model" not in attrs
        params = json.loads(attrs["gen_ai.request.parameters"])
        assert params["model"] == MODEL
        assert params["stream"] is True
        assert usage_attributes(attrs) == (USAGE_ATTRIBUTES if include_usage else {})
        assert len(requests) == 1
        assert str(requests[0].url) == "https://api.doubleword.ai/v1/chat/completions"
        body = json.loads(requests[0].content)
        if include_usage:
            assert body["stream_options"] == {"include_usage": True}
            assert params["stream_options"] == {"include_usage": True}
        else:
            assert "stream_options" not in body
        assert_key_separation(requests[0], receiver)


def test_async_flex_native_parameter_reaches_body_and_span(journey):
    signature = inspect.signature(openai.resources.chat.completions.Completions.create)
    assert "service_tier" in signature.parameters
    # Both tested SDKs accept the native parameter at runtime. The older SDK's
    # type annotation predates flex; branch on the version rather than exceptions.
    if SDK_VERSION >= (2, 0, 0):
        assert "flex" in str(signature.parameters["service_tier"].annotation)
    else:
        assert SDK_VERSION >= (1, 69, 0)
        assert "flex" not in str(signature.parameters["service_tier"].annotation)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    with journey() as (receiver, provider, _), mock_client(handler) as client:
        client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Flex control"}], service_tier="flex")
        _, attrs = only_span(receiver, provider)
        assert len(requests) == 1
        assert str(requests[0].url) == "https://api.doubleword.ai/v1/chat/completions"
        assert json.loads(requests[0].content)["service_tier"] == "flex"
        params = json.loads(attrs["gen_ai.request.parameters"])
        assert params["service_tier"] == "flex"
        assert params["model"] == MODEL
        assert attrs["gen_ai.request.model"] == RETURNED_MODEL
        assert_key_separation(requests[0], receiver)


@pytest.mark.parametrize("status,error_class", [(401, openai.AuthenticationError), (429, openai.RateLimitError)])
def test_errors_export_error_span_and_exception(journey, status, error_class):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=error_body(status))

    with journey() as (receiver, provider, _), mock_client(handler) as client:
        with pytest.raises(error_class) as caught:
            client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Error control"}])
        assert error_body(status)["error"]["message"] in str(caught.value)
        span, attrs = only_span(receiver, provider)
        assert span["name"] == "ChatCompletion"
        assert span["status"]["code"] == "STATUS_CODE_ERROR"
        assert error_class.__name__ in span["status"]["message"]
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert any(event["name"] == "exception" for event in span["events"])
        assert error_body(status)["error"]["message"] in json.dumps(span["events"])
        # traceai-openai records the model only from a non-streamed response;
        # flip this when the instrumentor records the request model.
        assert "gen_ai.request.model" not in attrs
        assert json.loads(attrs["gen_ai.request.parameters"])["model"] == MODEL
        assert usage_attributes(attrs) == {}
        assert len(requests) == 1  # The test client disables SDK retries.
        assert str(requests[0].url) == "https://api.doubleword.ai/v1/chat/completions"
        assert_key_separation(requests[0], receiver)


def test_hide_inputs_with_visible_control_and_provider_receipt(journey, monkeypatch):
    prompt = "unique-input-marker-doubleword"
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hidden).lower())
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion())

        with journey() as (receiver, provider, _), mock_client(handler) as client:
            client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}])
            span, attrs = only_span(receiver, provider)
            assert prompt in requests[0].content.decode()
            assert (prompt in json.dumps(receiver.spans())) is (not hidden)
            assert ANSWER in attrs["output.value"]  # Input masking does not hide outputs.
            assert attrs["gen_ai.provider.name"] == "openai"


@pytest.mark.parametrize("streaming", [False, True])
def test_hide_outputs_with_visible_control(journey, monkeypatch, streaming):
    marker = "unique-output-marker-doubleword"
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())

        def handler(request):
            if streaming:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream_bytes(text=marker))
            return httpx.Response(200, json=completion(text=marker))

        with journey() as (receiver, provider, _), mock_client(handler) as client:
            response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "visible-input-control"}], stream=streaming)
            if streaming:
                with response:
                    assert "".join(c.choices[0].delta.content or "" for c in response if c.choices) == marker
            else:
                assert response.choices[0].message.content == marker
            span, attrs = only_span(receiver, provider)
            assert (marker in json.dumps(span)) is (not hidden)
            assert "visible-input-control" in json.dumps(span)
            if hidden:
                assert attrs["output.value"] == "__REDACTED__"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("empty_text", [False, True])
def test_chat_content_and_no_raw_metadata_fallback(journey, monkeypatch, streaming, empty_text):
    marker = "unique-extra-response-marker"
    text = "" if empty_text else ANSWER
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())
        extra = {"provider_metadata": {"echoed_prompt": marker}}

        def handler(request):
            if streaming:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream_bytes(text=text, extra=extra))
            return httpx.Response(200, json=completion(text=text, extra=extra))

        with journey() as (receiver, provider, _), mock_client(handler) as client:
            response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": marker}], stream=streaming)
            # Positive control: extra metadata reaches the SDK, even though the
            # Chat Completions instrumentor exports content rather than raw JSON.
            if streaming:
                with response:
                    chunks = list(response)
                assert any(getattr(chunk, "provider_metadata", {}).get("echoed_prompt") == marker for chunk in chunks)
                assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == text
            else:
                assert response.provider_metadata["echoed_prompt"] == marker
                assert response.choices[0].message.content == text
            span, attrs = only_span(receiver, provider)
            assert marker not in json.dumps(span)
            assert "provider_metadata" not in json.dumps(span)
            if empty_text and not streaming:
                assert "output.value" not in attrs
            else:
                assert attrs["output.value"] == ("__REDACTED__" if hidden else text)


def test_embeddings_raw_vectors_survive_vector_hiding(journey, monkeypatch):
    for hidden in (False, True):
        monkeypatch.setenv("FI_HIDE_EMBEDDING_VECTORS", str(hidden).lower())
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=embeddings())

        with journey() as (receiver, provider, _), mock_client(handler) as client:
            response = client.embeddings.create(model="placeholder-embedding-model", input="embedding input control", encoding_format="float")
            assert response.data[0].embedding == VECTOR
            span, attrs = only_span(receiver, provider)
            assert span["name"] == "CreateEmbeddingResponse"
            assert attrs["gen_ai.span.kind"] == "EMBEDDING"
            assert attrs["gen_ai.provider.name"] == "openai"
            assert attrs["embedding.model_name"] == "placeholder-embedding-model-2026-09-01"
            assert "gen_ai.request.model" not in attrs
            assert json.loads(attrs["gen_ai.request.parameters"])["model"] == "placeholder-embedding-model"
            assert usage_attributes(attrs) == {"gen_ai.usage.input_tokens": 11, "gen_ai.usage.total_tokens": 11}
            raw = json.loads(attrs["embedding.embeddings"])
            assert raw["data"][0]["embedding"] == VECTOR
            vector_key = "embedding.embeddings.0.embedding.vector"
            assert (vector_key in attrs) is (not hidden)
            if not hidden:
                assert attrs[vector_key] == VECTOR
            assert len(requests) == 1
            assert str(requests[0].url) == "https://api.doubleword.ai/v1/embeddings"
            assert_key_separation(requests[0], receiver)


def test_check_base_url_preserves_allowed_spellings():
    for url in (
        "https://api.doubleword.ai/v1", "https://api.doubleword.ai/v1/",
        "HTTPS://API.DOUBLEWORD.AI/v1", "https://api.doubleword.ai./v1/",
        "https://api.doubleword.ai:443/v1", "http://127.0.0.1:12345/v1",
        "http://localhost:12345/v1/", "https://proxy.example.test/custom/v1?route=example#anchor",
    ):
        assert app.check_base_url(url) is url


def refused_urls():
    cases = []
    for host in ("api.doubleword.ai", "API.DOUBLEWORD.AI", "api.doubleword.ai."):
        for path in ("", "/", "/v1/chat/completions", "/chat/completions", "/v1/embeddings", "/v1/responses", "/V1", "/v1//", "/%76%31", "/v1/../v1"):
            cases.append((f"https://{host}{path}", "base URL"))
        for path in ("/batch", "/batches", "/batch/jobs", "/v1/batch", "/v1/batches", "/v1/batch/jobs"):
            cases.append((f"https://{host}{path}", "Batch API"))
        for path in ("/v1/messages", "/v1/messages/", "/messages"):
            cases.append((f"https://{host}{path}", "Anthropic Messages"))
        cases.append((f"http://{host}/v1", "HTTPS"))
        for suffix in ("?", "#", "?route=x", "#anchor"):
            cases.append((f"https://{host}/v1{suffix}", "query or fragment"))
    for char in (" ", "\t", "\r", "\n", "\x00", "\x7f", "\u00a0", "\u200b"):
        cases.append((f"https://api.doubleword.ai/v1{char}", "whitespace or control"))
    for dot in ("\u3002", "\uff0e", "\uff61"):
        cases.append((f"https://api{dot}doubleword.ai/v1", "ASCII host"))
    for host in ("xn--.example", "api..doubleword.ai", "-bad.example", "bad_.example", "api.doubleword.ai%2e", "api.doubleword.ai..", "API.DOUBLEWORD.AI..."):
        cases.append((f"https://{host}/v1", "host"))
    cases.append(("https://\u00e1pi.doubleword.ai/v1", "ASCII host"))
    for origin in ("https://api.doubleword.ai", "https://proxy.example.test", "http://127.0.0.1:12345"):
        scheme, host = origin.split("://")
        cases.append((f"{scheme}://private-credential-marker:private-password-marker@{host}/v1", "credentials"))
        cases.append((f"{scheme}://@{host}/v1", "credentials"))
    cases.extend((
        ("", "absolute URL"), ("api.doubleword.ai/v1", "absolute"),
        ("ftp://api.doubleword.ai/v1", "HTTP(S)"),
        ("https:///v1", "host"), ("https://[invalid/v1", "valid absolute"),
        ("https://api.doubleword.ai:invalid/v1", "valid absolute"),
    ))
    return cases


def test_every_refused_url_exits_before_tracing_or_network(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid URL must exit before tracing or client creation")

    monkeypatch.setattr(app, "setup_tracing", unexpected)
    monkeypatch.setattr(app, "make_client", unexpected)
    with Receiver() as receiver, FakeOpenAI() as fake:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        for url, reason in refused_urls():
            with pytest.raises(ValueError) as caught:
                app.check_base_url(url)
            message = str(caught.value)
            assert app.BASE_URL_ENV in message
            assert reason in message
            assert "\n" not in message
            assert "private-credential-marker" not in message
            assert "private-password-marker" not in message
            if not url:
                # Empty override selects the documented default by contract.
                continue
            # OS environment values cannot contain NUL. An in-memory mapping
            # exercises main's preflight for that spelling without calling putenv.
            invalid_environment = dict(os.environ)
            invalid_environment[app.BASE_URL_ENV] = url
            with monkeypatch.context() as context:
                context.setattr(app.os, "environ", invalid_environment)
                assert app.main([]) == 2
            output = capsys.readouterr()
            assert output.out == ""
            assert message in output.err
            assert "private-credential-marker" not in output.err
            assert receiver.spans() == []
            assert receiver.requests() == []
            assert fake.requests() == []


@pytest.mark.parametrize("missing", [app.API_KEY_ENV, app.MODEL_ENV])
@pytest.mark.parametrize("empty", [False, True])
def test_missing_model_or_vendor_key_exits_before_tracing(monkeypatch, capsys, missing, empty):
    if empty:
        monkeypatch.setenv(missing, "")
    else:
        monkeypatch.delenv(missing)

    def unexpected(*args, **kwargs):
        pytest.fail("missing configuration must exit before tracing")

    monkeypatch.setattr(app, "setup_tracing", unexpected)
    with Receiver() as receiver, FakeOpenAI() as fake:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv(app.BASE_URL_ENV, fake.origin + "/v1")
        assert app.main([]) == 2
        assert missing in capsys.readouterr().err
        assert receiver.spans() == []
        assert receiver.requests() == []
        assert fake.requests() == []


def test_make_client_override_precedence_and_missing_key(monkeypatch):
    monkeypatch.setenv(app.BASE_URL_ENV, "http://127.0.0.1:12345/custom/v1")
    with app.make_client() as client:
        assert str(client.base_url) == "http://127.0.0.1:12345/custom/v1/"
    with app.make_client(base_url="https://api.doubleword.ai/v1/", api_key="placeholder-explicit-key") as client:
        assert str(client.base_url) == "https://api.doubleword.ai/v1/"
        assert client.api_key == "placeholder-explicit-key"
    monkeypatch.delenv(app.API_KEY_ENV)
    with pytest.raises(KeyError) as caught:
        app.make_client()
    assert caught.value.args == (app.API_KEY_ENV,)


def child_environment(receiver, fake, directory):
    env = dict(os.environ)
    # Use the packages this process imported, so published-wheel runs stay valid.
    roots = [RECIPE / "tests/loopback_guard", RECIPE / "src",
             Path(openai.__file__).resolve().parent.parent,
             Path(fi_instrumentation.__file__).resolve().parent.parent,
             Path(traceai_openai.__file__).resolve().parent.parent,
             Path(harness.__file__).resolve().parent.parent]
    if SDK_VERSION < (2, 0, 0):
        import distro  # The SDK floor has this extra dependency.
        roots.append(Path(distro.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(str(root) for root in roots))
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "FI_API_KEY": FI_KEY, "FI_SECRET_KEY": FI_SECRET,
        "FI_BASE_URL": receiver.origin, app.API_KEY_ENV: VENDOR_KEY,
        app.MODEL_ENV: MODEL, app.BASE_URL_ENV: fake.origin + "/v1",
        "LOOPBACK_GUARD_LOG": str(directory / "guard.log"),
        "LOOPBACK_GUARD_READY": str(directory / "guard.ready"),
    })
    return env


@pytest.mark.parametrize("streaming", [False, True])
def test_app_subprocess_loopback_chat_and_stream(streaming):
    with Receiver() as receiver, FakeOpenAI() as fake, tempfile.TemporaryDirectory(dir=RECIPE, prefix=".test-run-") as temporary:
        directory = Path(temporary)
        env = child_environment(receiver, fake, directory)
        argv = [sys.executable, str(RECIPE / "src/app.py"), "--prompt", "subprocess-prompt-control"]
        if streaming:
            argv.append("--stream")
        else:
            # A CLI model also works when the model environment variable is absent.
            env.pop(app.MODEL_ENV)
            argv.extend(["--model", MODEL])
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ANSWER
        assert directory.joinpath("guard.ready").read_text() == "installed\n"
        assert directory.joinpath("guard.log").read_text() == ""
        requests = fake.requests()
        assert len(requests) == 1
        assert requests[0]["path"] == "/v1/chat/completions"
        assert requests[0]["headers"]["authorization"] == f"Bearer {VENDOR_KEY}"
        assert requests[0]["headers"]["user-agent"] == f"OpenAI/Python {openai.__version__}"
        assert "x-api-key" not in requests[0]["headers"]
        assert "x-secret-key" not in requests[0]["headers"]
        assert requests[0]["body"]["model"] == MODEL
        assert requests[0]["body"]["stream"] is streaming
        assert requests[0]["body"]["messages"][0]["content"] == "subprocess-prompt-control"
        spans = receiver.spans()
        assert len(spans) == 1
        attrs = attributes(spans[0])
        assert spans[0]["name"] == "ChatCompletion"
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert json.loads(attrs["gen_ai.request.parameters"])["model"] == MODEL
        if streaming:
            assert "gen_ai.request.model" not in attrs
            assert usage_attributes(attrs) == {}
        else:
            assert attrs["gen_ai.request.model"] == RETURNED_MODEL
            assert usage_attributes(attrs) == USAGE_ATTRIBUTES
        assert ANSWER in attrs["output.value"]
        exports = receiver.requests()
        assert len(exports) == 1
        assert exports[0]["path"] == "/tracer/v1/traces"
        assert exports[0]["headers"]["x-api-key"] == FI_KEY
        assert exports[0]["headers"]["x-secret-key"] == FI_SECRET
        assert exports[0]["resource_attributes"][0]["project_name"] == "doubleword-example"
        assert VENDOR_KEY not in json.dumps(spans + exports)
        assert VENDOR_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("operation", ["dns", "connect", "connect_ex"])
def test_loopback_guard_refuses_real_host_before_dns(operation):
    with Receiver() as receiver, FakeOpenAI() as fake, tempfile.TemporaryDirectory(dir=RECIPE, prefix=".test-run-") as temporary:
        directory = Path(temporary)
        env = child_environment(receiver, fake, directory)
        calls = {
            "dns": "socket.getaddrinfo('api.doubleword.ai', 443)",
            "connect": "socket.socket().connect(('api.doubleword.ai', 443))",
            "connect_ex": "socket.socket().connect_ex(('api.doubleword.ai', 443))",
        }
        # Positive control: DNS for loopback is permitted under the same guard.
        code = "import socket; assert socket.getaddrinfo('127.0.0.1', 80); " + calls[operation]
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode != 0
        assert "loopback guard refused non-loopback host" in result.stderr
        assert directory.joinpath("guard.ready").read_text() == "installed\n"
        assert directory.joinpath("guard.log").read_text() == "Refused non-loopback host\n"
        assert fake.requests() == []
        assert receiver.spans() == []


def test_readme_requirements_and_public_contract():
    readme = (RECIPE / "README.md").read_text()
    assert readme.startswith("# Doubleword (OpenAI-compatible) with traceAI")
    assert app.DEFAULT_BASE_URL in readme
    assert "provider field says `openai`" in readme
    for name in (app.API_KEY_ENV, app.BASE_URL_ENV, app.MODEL_ENV, "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_EMBEDDING_VECTORS"):
        assert name in readme
    assert "gen_ai.request.model" in readme
    assert "service_tier=\"flex\"" in readme
    assert "embedding.embeddings" in readme
    assert "verbose=False" in readme
    assert "TBD" not in readme
    assert "https://doubleword.ai/llms.txt" in readme
    assert "https://app.doubleword.ai/api-keys" in readme
    assert "not suitable for production workloads" in readme
    for py_path in ("python/examples/doubleword/src:python:python/frameworks/openai:python/tests", "python/examples/doubleword/src:python/tests"):
        assert f'PYTHONPATH="{py_path}"' in readme
    assert readme.count("pytest python/examples/doubleword/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs") == 2
    assert "--with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0'" in readme
    requirements = (RECIPE / "requirements.txt").read_text()
    assert [line for line in requirements.splitlines() if line and not line.startswith("#")] == ["openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0"]
    assert not re.search(r"\b(?:sk|fi)-[a-zA-Z0-9]{16,}\b", readme)
    for path in RECIPE.rglob("*"):
        if path.is_file():
            assert not re.search(r"\b[A-Z]{2,5}-\d{3,}\b", path.read_text()), str(path)
