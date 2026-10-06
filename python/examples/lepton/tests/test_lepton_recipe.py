"""Fixture-only recipe contracts; syntactic endpoint hosts are never contacted."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from contextlib import contextmanager
from urllib.parse import quote, urlsplit

import fi_instrumentation
import httpx
import openai
import pytest
import traceai_openai
from harness import Receiver
from traceai_openai import OpenAIInstrumentor

TEST_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TEST_DIR.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = load_module("lepton_recipe_app", RECIPE_DIR / "src" / "app.py")
fake = load_module("lepton_fake_openai", TEST_DIR / "_fake_openai.py")
TEST_BASE_URL = "https://endpoint.example.invalid/v1"
XENON_TEST_BASE_URL = "https://ws0000-example.xenon.lepton.run/v1"
MODEL = "nvidia/Nemotron-Research-Reasoning-Qwen-1.5B"
VENDOR_KEY = "placeholder-lepton-key"
FI_KEY = "placeholder-futureagi-key"
FI_SECRET = "placeholder-futureagi-secret"
HIDE_FLAGS = (
    "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_LLM_INVOCATION_PARAMETERS",
    "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_IMAGES",
    "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_EMBEDDING_VECTORS",
)
INVALID_HOST_URLS = [
    "https://endpoint.example.invalid\u00a0",
    "https://endpoint.example.invalid\u200b",
    *[f"https://endpoint{separator}example.invalid" for separator in ("\u3002", "\uff0e", "\uff61")],
    "https://xn--.example.invalid",
    "https://" + "a" * 64 + ".example.invalid",
]
ALLOWED_URLS = [
    (TEST_BASE_URL, TEST_BASE_URL + "/"),
    ("https://endpoint.example.invalid", "https://endpoint.example.invalid"),
    ("https://endpoint.example.invalid/custom/root/", "https://endpoint.example.invalid/custom/root/"),
    ("HTTPS://ENDPOINT.EXAMPLE.INVALID./Custom/Path", "https://endpoint.example.invalid./Custom/Path/"),
    ("http://127.0.0.1:12345/inference", "http://127.0.0.1:12345/inference/"),
    (XENON_TEST_BASE_URL, XENON_TEST_BASE_URL + "/"),
    (XENON_TEST_BASE_URL + "/", XENON_TEST_BASE_URL + "/"),
    ("HTTPS://WS0000-EXAMPLE.XENON.LEPTON.RUN./v1", "https://ws0000-example.xenon.lepton.run./v1/"),
    ("https://nested.sdxl.lepton.run/v1", "https://nested.sdxl.lepton.run/v1/"),
]
REFUSED_URLS = [
    (f"https://{host}/v1", reason)
    for host, reason in (
        ("api.lepton.ai", "Legacy"), ("llm.lepton.run", "Legacy"),
        ("sdxl.lepton.run", "Legacy"),
        ("dashboard.dgxc-lepton.nvidia.com", "Console"), ("dashboard.lepton.ai", "Console"),
        ("API.LEPTON.AI", "Legacy"), ("api.lepton.ai.", "Legacy"),
        ("LLM.LEPTON.RUN.", "Legacy"), ("SDXL.LEPTON.RUN.", "Legacy"),
        ("DASHBOARD.DGXC-LEPTON.NVIDIA.COM.", "Console"), ("DASHBOARD.LEPTON.AI.", "Console"),
    )
] + [
    (value, "placeholder")
    for value in (
        app.DOCUMENTED_BASE_URL_FORMS[0],
        "https://endpoint.example.invalid/ENDPOINT_URL",
        "https://endpoint.example.invalid/<replace-me>",
        "https://endpoint.example.invalid/replace-me>",
        quote(app.DOCUMENTED_BASE_URL_FORMS[0], safe=""),
        quote(quote(app.DOCUMENTED_BASE_URL_FORMS[0], safe=""), safe=""),
        "https://endpoint.example.invalid/%45NDPOINT_URL",
        "https://endpoint.example.invalid/%2545NDPOINT_URL",
    )
] + [
    ("http://ws0000-example.xenon.lepton.run/v1", "https"),
    ("HTTP://WS0000-EXAMPLE.XENON.LEPTON.RUN./v1", "https"),
    ("https://ws0000-example.xenon.lepton.run", "append /v1"),
    ("https://ws0000-example.xenon.lepton.run/", "append /v1"),
    ("https://WS0000-EXAMPLE.XENON.LEPTON.RUN.", "append /v1"),
    (XENON_TEST_BASE_URL + "?placeholder=query", "query or fragment"),
    (XENON_TEST_BASE_URL + "#placeholder-fragment", "query or fragment"),
    (XENON_TEST_BASE_URL + "?", "query or fragment"),
    (XENON_TEST_BASE_URL + "#", "query or fragment"),
    ("https://placeholder-user:placeholder-password@endpoint.example.invalid/v1", "credentials"),
    ("https://placeholder-user@ws0000-example.xenon.lepton.run/v1", "credentials"),
    ("http://placeholder-user@127.0.0.1:12345/v1", "credentials"),
    ("https://@endpoint.example.invalid/v1", "credentials"),
    *[(url, "valid HTTP endpoint base URL") for url in INVALID_HOST_URLS],
]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in HIDE_FLAGS + ("FI_PII_REDACTION", "OPENAI_LOG"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv(app.BASE_URL_ENV, TEST_BASE_URL)
    monkeypatch.setenv(app.API_KEY_ENV, VENDOR_KEY)
    monkeypatch.setenv(app.MODEL_ENV, MODEL)
    # Also protect the test process: only the receiver and fake may use sockets.
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(host):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        if host not in ("127.0.0.1", "localhost"):
            raise RuntimeError("Test refused non-loopback network access")

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


@contextmanager
def tracing(monkeypatch, project_name):
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        provider = app.setup_tracing(project_name)
        try:
            yield receiver, provider
        finally:
            provider.force_flush()
            OpenAIInstrumentor().uninstrument()
            provider.shutdown()
            for sig, handler in previous_signals.items():
                signal.signal(sig, handler)


def attributes(span):
    return {
        item["key"]: next(iter(item["value"].values()))
        for item in span.get("attributes", [])
    }


def one_span(receiver, provider):
    assert provider.force_flush()
    spans = receiver.spans()
    assert len(spans) == 1
    assert spans[0]["name"] == "ChatCompletion"
    values = attributes(spans[0])
    assert values["gen_ai.span.kind"] == "LLM"
    assert values["gen_ai.provider.name"] == "openai"
    return spans[0], values


def telemetry(receiver):
    return json.dumps({
        "spans": receiver.spans(),
        "resources": [request["resource_attributes"] for request in receiver.requests()],
    })


def assert_key_separation(receiver, request):
    # Positive control: the key really was used on the vendor request.
    assert request.headers["authorization"] == f"Bearer {VENDOR_KEY}"
    assert "x-api-key" not in request.headers
    assert "x-secret-key" not in request.headers
    assert FI_KEY not in str(request.headers)
    assert FI_SECRET not in str(request.headers)
    assert receiver.spans()
    assert VENDOR_KEY not in telemetry(receiver)
    exports = receiver.requests()
    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET
        assert VENDOR_KEY not in json.dumps(export["headers"])


def mock_client(handler):
    return app.make_client(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def chat(client, **kwargs):
    return client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": "What is a comet?"}], **kwargs,
    )


def child_environment(tmp_path, receiver, fake_server):
    environment = os.environ.copy()
    # Use actual imported package locations for source and published-wheel runs.
    package_paths = [
        str(Path(fi_instrumentation.__file__).resolve().parent.parent),
        str(Path(traceai_openai.__file__).resolve().parent.parent),
    ]
    environment.update({
        "PYTHONPATH": os.pathsep.join(dict.fromkeys([
            str(TEST_DIR / "loopback_guard"), str(RECIPE_DIR / "src"),
            *package_paths, str(Path(sys.modules["harness"].__file__).resolve().parent.parent),
        ])),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FI_BASE_URL": receiver.origin,
        "FI_API_KEY": FI_KEY,
        "FI_SECRET_KEY": FI_SECRET,
        app.BASE_URL_ENV: fake_server.base_url,
        app.API_KEY_ENV: VENDOR_KEY,
        app.MODEL_ENV: MODEL,
        "LOOPBACK_GUARD_LOG": str(tmp_path / "guard.log"),
        "LOOPBACK_GUARD_READY": str(tmp_path / "guard.ready"),
        "NO_PROXY": "*",
    })
    Path(environment["LOOPBACK_GUARD_LOG"]).write_text("", encoding="utf-8")
    return environment


def run_child(environment, *args):
    return subprocess.run([sys.executable, *args], env=environment, capture_output=True, text=True, timeout=30)


def assert_guard_installed(environment):
    assert Path(environment["LOOPBACK_GUARD_READY"]).read_text() == "installed\n"


def test_documented_forms_require_customer_configuration(monkeypatch):
    assert app.DEFAULT_BASE_URL is None
    assert app.DOCUMENTED_BASE_URL_FORMS == ("<ENDPOINT_URL from the API tab>/v1",)
    assert app.API_KEY_ENV == "LEPTON_API_TOKEN"
    assert app.BASE_URL_ENV == "LEPTON_ENDPOINT_URL"
    assert app.MODEL_ENV == "LEPTON_MODEL"
    readme = (RECIPE_DIR / "README.md").read_text()
    for form in app.DOCUMENTED_BASE_URL_FORMS:
        assert form in readme
        with pytest.raises(ValueError, match="placeholder"):
            app.check_base_url(form)
    monkeypatch.delenv(app.BASE_URL_ENV)
    with pytest.raises(ValueError, match=app.BASE_URL_ENV):
        app.make_client()


@pytest.mark.parametrize("url,sdk_url", ALLOWED_URLS)
def test_allowed_base_urls_are_returned_unchanged(url, sdk_url):
    assert app.check_base_url(url) == url
    with app.make_client(base_url=url) as client:
        # Pin the SDK's serialized URL, including its treatment of an empty path.
        assert str(client.base_url) == sdk_url


@pytest.mark.parametrize("url,reason", REFUSED_URLS)
def test_refused_base_urls_name_reason_without_disclosing_url(url, reason):
    with pytest.raises(ValueError) as caught:
        app.check_base_url(url)
    message = str(caught.value)
    assert reason in message
    assert url not in message
    if host := urlsplit(url).hostname:
        assert host not in message
    assert "\n" not in message
    if reason == "append /v1":
        assert message == "append /v1 to the endpoint URL (NVIDIA documents <ENDPOINT_URL>/v1/chat/completions)"
    elif url in INVALID_HOST_URLS:
        assert message == "Set LEPTON_ENDPOINT_URL to a valid HTTP endpoint base URL."
        assert caught.value.__suppress_context__ is True
    elif reason == "credentials":
        assert "placeholder-user" not in message
        assert "placeholder-password" not in message


@pytest.mark.parametrize("key", [None, "", "   "])
def test_make_client_requires_nonempty_vendor_key(monkeypatch, key):
    if key is None:
        monkeypatch.delenv(app.API_KEY_ENV)
    else:
        monkeypatch.setenv(app.API_KEY_ENV, key)
    with pytest.raises(ValueError, match=app.API_KEY_ENV) as caught:
        app.make_client()
    assert str(caught.value) == f"Set {app.API_KEY_ENV} to the non-empty token shown in the API tab."


@pytest.mark.parametrize("base_url", [TEST_BASE_URL, XENON_TEST_BASE_URL])
def test_chat_completion_contract(monkeypatch, base_url):
    monkeypatch.setenv(app.BASE_URL_ENV, base_url)
    requests = []

    def handler(request):
        requests.append(request)
        assert json.loads(request.content)["model"] == MODEL
        return httpx.Response(200, json=fake.completion(MODEL))

    project_name = "lepton-chat-xenon" if base_url == XENON_TEST_BASE_URL else "lepton-chat-invalid"
    with tracing(monkeypatch, project_name) as (receiver, provider):
        with mock_client(handler) as client:
            assert str(client.base_url) == base_url + "/"
            assert chat(client).choices[0].message.content == fake.ANSWER
        span, values = one_span(receiver, provider)
        assert values["gen_ai.request.model"] == MODEL
        assert values["output.value"] == fake.ANSWER
        assert span["status"]["code"] == "STATUS_CODE_OK"
        assert {key: int(value) for key, value in values.items() if key.startswith("gen_ai.usage.")} == {
            "gen_ai.usage.input_tokens": 11, "gen_ai.usage.output_tokens": 13, "gen_ai.usage.total_tokens": 24,
        }
        assert len(requests) == 1
        assert str(requests[0].url) == base_url + "/chat/completions"
        assert_key_separation(receiver, requests[0])
        assert base_url not in telemetry(receiver)
        assert urlsplit(base_url).hostname not in telemetry(receiver)
        assert all(resource["project_name"] == project_name
                   for export in receiver.requests() for resource in export["resource_attributes"])


def test_missing_usage_is_omitted(monkeypatch):
    with tracing(monkeypatch, "lepton-missing-usage") as (receiver, provider):
        for has_usage in (True, False):
            receiver.clear()
            with mock_client(lambda request: httpx.Response(200, json=fake.completion(MODEL, usage=has_usage))) as client:
                chat(client)
            _, values = one_span(receiver, provider)
            usage = {key: int(value) for key, value in values.items() if key.startswith("gen_ai.usage.")}
            if has_usage:
                assert usage == {"gen_ai.usage.input_tokens": 11, "gen_ai.usage.output_tokens": 13, "gen_ai.usage.total_tokens": 24}
            else:
                assert usage == {}


@pytest.mark.parametrize("include_usage", [False, True])
def test_stream_accumulates_text_and_pins_usage(monkeypatch, include_usage):
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body.get("stream_options") == ({"include_usage": True} if include_usage else None)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=fake.stream(MODEL, include_usage=include_usage))

    with tracing(monkeypatch, f"lepton-stream-{include_usage}") as (receiver, provider):
        with mock_client(handler) as client:
            kwargs = {"stream_options": {"include_usage": True}} if include_usage else {}
            text = "".join(chunk.choices[0].delta.content or "" for chunk in chat(client, stream=True, **kwargs) if chunk.choices)
        assert text == fake.ANSWER
        _, values = one_span(receiver, provider)
        assert values["output.value"] == fake.ANSWER
        # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
        assert "gen_ai.request.model" not in values
        assert json.loads(values["gen_ai.request.parameters"])["model"] == MODEL
        usage = {key: int(value) for key, value in values.items() if key.startswith("gen_ai.usage.")}
        assert usage == ({"gen_ai.usage.input_tokens": 11, "gen_ai.usage.output_tokens": 13, "gen_ai.usage.total_tokens": 24} if include_usage else {})
        assert len(requests) == 1
        assert str(requests[0].url) == TEST_BASE_URL + "/chat/completions"
        assert_key_separation(receiver, requests[0])


def test_authentication_error_is_recorded_without_vendor_key(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401, json=fake.ERROR)

    with tracing(monkeypatch, "lepton-authentication-error") as (receiver, provider):
        with mock_client(handler) as client, pytest.raises(openai.AuthenticationError) as caught:
            chat(client)
        span, values = one_span(receiver, provider)
        assert span["status"]["code"] == "STATUS_CODE_ERROR"
        assert "Unauthorized" in span["status"]["message"]
        exceptions = [event for event in span["events"] if event["name"] == "exception"]
        assert len(exceptions) == 1
        assert "Unauthorized" in json.dumps(exceptions)
        assert VENDOR_KEY not in str(caught.value)
        # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
        assert "gen_ai.request.model" not in values
        assert json.loads(values["gen_ai.request.parameters"])["model"] == MODEL
        assert not any(key.startswith("gen_ai.usage.") for key in values)
        assert len(requests) == 1
        assert_key_separation(receiver, requests[0])


@pytest.mark.parametrize("flag,marker", [
    ("FI_HIDE_INPUTS", "unique-lepton-private-prompt"), ("FI_HIDE_OUTPUTS", "unique-lepton-private-answer"),
])
def test_privacy_flags_hide_text_with_visible_control(monkeypatch, flag, marker):
    for hidden in (False, True):
        monkeypatch.setenv(flag, str(hidden).lower())
        requests = []
        prompt = marker if flag == "FI_HIDE_INPUTS" else "ordinary-lepton-prompt"
        answer = marker if flag == "FI_HIDE_OUTPUTS" else fake.ANSWER

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=fake.completion(MODEL, answer=answer))

        with tracing(monkeypatch, f"lepton-privacy-{flag}-{hidden}") as (receiver, provider):
            with mock_client(handler) as client:
                response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}])
            one_span(receiver, provider)
            assert json.loads(requests[0].content)["messages"][0]["content"] == prompt
            assert response.choices[0].message.content == answer
            assert (marker in telemetry(receiver)) is (not hidden)


@pytest.mark.parametrize("journey", ["chat", "stream", "error"])
@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.parametrize("base_url", [TEST_BASE_URL, XENON_TEST_BASE_URL])
def test_endpoint_url_and_host_are_not_exported(monkeypatch, journey, hide, base_url):
    monkeypatch.setenv(app.BASE_URL_ENV, base_url)
    for flag in HIDE_FLAGS:
        monkeypatch.setenv(flag, str(hide).lower())
    requests = []

    def handler(request):
        requests.append(request)
        if journey == "error":
            return httpx.Response(401, json=fake.ERROR)
        if journey == "stream":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=fake.stream(MODEL))
        return httpx.Response(200, json=fake.completion(MODEL))

    host_label = "xenon" if base_url == XENON_TEST_BASE_URL else "invalid"
    with tracing(monkeypatch, f"lepton-url-privacy-{host_label}-{journey}-{hide}") as (receiver, provider):
        with mock_client(handler) as client:
            if journey == "error":
                with pytest.raises(openai.AuthenticationError):
                    chat(client)
            elif journey == "stream":
                assert list(chat(client, stream=True))
            else:
                assert chat(client).model == MODEL
        _, values = one_span(receiver, provider)
        # Positive controls: the endpoint was used, and real telemetry was exported.
        assert len(requests) == 1
        assert str(requests[0].url) == base_url + "/chat/completions"
        assert receiver.requests()[0]["resource_attributes"]
        exported = telemetry(receiver)
        assert base_url not in exported
        assert urlsplit(base_url).hostname not in exported
        assert not {"server.address", "url.full", "http.url", "openai.base_url"}.intersection(values)
        if hide:
            assert "gen_ai.request.parameters" not in values
        else:
            assert json.loads(values["gen_ai.request.parameters"])["model"] == MODEL
        assert_key_separation(receiver, requests[0])


@pytest.mark.parametrize("stream", [False, True])
def test_app_subprocess_is_loopback_only(monkeypatch, tmp_path, stream):
    with Receiver() as receiver, fake.FakeOpenAI() as server:
        environment = child_environment(tmp_path, receiver, server)
        arguments = ["--prompt", "unique-lepton-cli-prompt"]
        if stream:
            del environment[app.MODEL_ENV]
            arguments += ["--stream", "--model", MODEL]
        result = run_child(environment, str(RECIPE_DIR / "src" / "app.py"), *arguments)
        assert result.returncode == 0, result.stderr
        assert fake.ANSWER in result.stdout
        assert server.base_url not in result.stdout + result.stderr
        assert VENDOR_KEY not in result.stdout + result.stderr
        assert_guard_installed(environment)
        assert Path(environment["LOOPBACK_GUARD_LOG"]).read_text() == ""
        assert len(server.requests) == 1
        request = server.requests[0]
        assert request["path"] == "/v1/chat/completions"
        assert request["headers"]["authorization"] == f"Bearer {VENDOR_KEY}"
        assert "x-api-key" not in request["headers"]
        assert "x-secret-key" not in request["headers"]
        assert request["body"]["model"] == MODEL
        assert request["body"]["stream"] is stream
        assert request["body"]["messages"] == [{"role": "user", "content": "unique-lepton-cli-prompt"}]
        spans = receiver.spans()
        assert len(spans) == 1
        assert spans[0]["name"] == "ChatCompletion"
        values = attributes(spans[0])
        assert values["gen_ai.span.kind"] == "LLM"
        assert values["gen_ai.provider.name"] == "openai"
        assert fake.ANSWER in values["output.value"]
        if stream:
            # traceai-openai records the model only from a non-streamed response; flip this when the instrumentor records the request model
            assert "gen_ai.request.model" not in values
            assert json.loads(values["gen_ai.request.parameters"])["model"] == MODEL
        else:
            assert values["gen_ai.request.model"] == MODEL
        assert VENDOR_KEY not in telemetry(receiver)
        for export in receiver.requests():
            assert export["path"] == "/tracer/v1/traces"
            assert export["headers"]["x-api-key"] == FI_KEY
            assert export["headers"]["x-secret-key"] == FI_SECRET
            assert VENDOR_KEY not in json.dumps(export["headers"])
            assert all(resource["project_name"] == "lepton-openai-recipe" for resource in export["resource_attributes"])


@pytest.mark.parametrize("operation", ["dns", "connect", "connect_ex"])
@pytest.mark.parametrize("host", [urlsplit(TEST_BASE_URL).hostname, urlsplit(XENON_TEST_BASE_URL).hostname])
def test_guard_refuses_syntactic_host_before_dns(tmp_path, operation, host):
    with Receiver() as receiver, fake.FakeOpenAI() as server:
        environment = child_environment(tmp_path, receiver, server)
        # The positive control proves that the same guard permits a loopback request.
        positive = run_child(environment, "-c", "import socket; s = socket.create_connection(('127.0.0.1', " + str(server._server.server_port) + ")); s.close()")
        assert positive.returncode == 0, positive.stderr
        assert_guard_installed(environment)
        assert Path(environment["LOOPBACK_GUARD_LOG"]).read_text() == ""
        code = "import socket; " + {
            "dns": f"socket.getaddrinfo({host!r}, 443)",
            "connect": f"socket.socket().connect(({host!r}, 443))",
            "connect_ex": f"socket.socket().connect_ex(({host!r}, 443))",
        }[operation]
        refused = run_child(environment, "-c", code)
        assert refused.returncode != 0
        assert "Loopback guard refused" in refused.stderr
        assert Path(environment["LOOPBACK_GUARD_LOG"]).read_text() == "REFUSED non-loopback host\n"
        assert server.requests == []
        assert receiver.spans() == []


@pytest.mark.parametrize("variable,value", [
    (app.BASE_URL_ENV, None), (app.BASE_URL_ENV, ""), (app.MODEL_ENV, None),
    (app.MODEL_ENV, ""), (app.API_KEY_ENV, None), (app.API_KEY_ENV, ""), (app.API_KEY_ENV, "   "),
])
def test_main_missing_configuration_exits_before_tracing(monkeypatch, capsys, variable, value):
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)

    def forbidden():
        pytest.fail("Tracing started before configuration validation")

    monkeypatch.setattr(app, "setup_tracing", forbidden)
    assert app.main([]) == 2
    captured = capsys.readouterr()
    assert variable in captured.err
    assert TEST_BASE_URL not in captured.out + captured.err
    assert VENDOR_KEY not in captured.out + captured.err


@pytest.mark.parametrize("url,reason", [
    ("https://api.lepton.ai/v1", "Legacy"),
    ("https://dashboard.dgxc-lepton.nvidia.com/v1", "Console"),
    (quote(quote(app.DOCUMENTED_BASE_URL_FORMS[0], safe=""), safe=""), "placeholder"),
    ("http://ws0000-example.xenon.lepton.run/v1", "https"),
    ("https://ws0000-example.xenon.lepton.run", "append /v1"),
    (XENON_TEST_BASE_URL + "?placeholder=query", "query or fragment"),
])
def test_main_refuses_url_without_tracing_or_network(monkeypatch, capsys, tmp_path, url, reason):
    with Receiver() as receiver, fake.FakeOpenAI() as server:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv(app.BASE_URL_ENV, url)

        def forbidden():
            pytest.fail("Tracing started for a refused URL")

        monkeypatch.setattr(app, "setup_tracing", forbidden)
        assert app.main([]) == 2
        captured = capsys.readouterr()
        assert reason in captured.err
        assert url not in captured.out + captured.err
        environment = child_environment(tmp_path, receiver, server)
        environment[app.BASE_URL_ENV] = url
        child = run_child(environment, str(RECIPE_DIR / "src" / "app.py"))
        assert child.returncode == 2
        assert reason in child.stderr
        assert url not in child.stdout + child.stderr
        assert_guard_installed(environment)
        assert Path(environment["LOOPBACK_GUARD_LOG"]).read_text() == ""
        assert server.requests == []
        assert receiver.spans() == []
        assert receiver.requests() == []


@pytest.mark.parametrize("url", INVALID_HOST_URLS)
def test_main_rejects_invalid_hosts_without_disclosure(monkeypatch, capsys, tmp_path, url):
    host = urlsplit(url).hostname
    with Receiver() as receiver, fake.FakeOpenAI() as server:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv(app.BASE_URL_ENV, url)

        def forbidden():
            pytest.fail("Tracing started for an invalid host")

        monkeypatch.setattr(app, "setup_tracing", forbidden)
        assert app.main([]) == 2
        captured = capsys.readouterr()
        assert captured.err == "Set LEPTON_ENDPOINT_URL to a valid HTTP endpoint base URL.\n"
        assert host not in captured.out + captured.err
        assert "Traceback" not in captured.err
        environment = child_environment(tmp_path, receiver, server)
        environment[app.BASE_URL_ENV] = url
        child = run_child(environment, str(RECIPE_DIR / "src" / "app.py"))
        assert child.returncode == 2
        assert child.stderr.strip().endswith("Set LEPTON_ENDPOINT_URL to a valid HTTP endpoint base URL.")
        assert host not in child.stdout + child.stderr
        assert "Traceback" not in child.stderr
        assert_guard_installed(environment)
        assert Path(environment["LOOPBACK_GUARD_LOG"]).read_text() == ""
        assert server.requests == []
        assert receiver.spans() == []
        assert receiver.requests() == []


def test_app_error_does_not_print_endpoint_url(tmp_path):
    with Receiver() as receiver, fake.FakeOpenAI(error=True) as server:
        environment = child_environment(tmp_path, receiver, server)
        result = run_child(environment, str(RECIPE_DIR / "src" / "app.py"))
        assert result.returncode == 1
        assert "Endpoint request failed" in result.stderr
        assert server.base_url not in result.stdout + result.stderr
        assert VENDOR_KEY not in result.stdout + result.stderr
        assert_guard_installed(environment)
        assert Path(environment["LOOPBACK_GUARD_LOG"]).read_text() == ""
        assert len(server.requests) == 1
        assert server.requests[0]["headers"]["authorization"] == f"Bearer {VENDOR_KEY}"
        spans = receiver.spans()
        assert len(spans) == 1
        assert spans[0]["status"]["code"] == "STATUS_CODE_ERROR"


@pytest.mark.parametrize("error_type", [openai.OpenAIError, RuntimeError])
def test_main_redacts_client_errors_containing_endpoint_url(monkeypatch, capsys, error_type):
    flushed = []

    class Provider:
        def force_flush(self):
            flushed.append(True)

    error = error_type(f"Request to {TEST_BASE_URL} failed using {VENDOR_KEY}")
    assert TEST_BASE_URL in str(error)
    assert VENDOR_KEY in str(error)

    def failed_client(**kwargs):
        assert kwargs == {"base_url": TEST_BASE_URL, "api_key": VENDOR_KEY}
        raise error

    monkeypatch.setattr(app, "setup_tracing", Provider)
    monkeypatch.setattr(app, "make_client", failed_client)
    assert app.main([]) == 1
    captured = capsys.readouterr()
    assert "Endpoint request failed" in captured.err
    assert TEST_BASE_URL not in captured.out + captured.err
    assert urlsplit(TEST_BASE_URL).hostname not in captured.out + captured.err
    assert "Traceback" not in captured.err
    assert VENDOR_KEY not in captured.out + captured.err
    assert flushed == [True]


def test_readme_and_requirements_pin_customer_contract():
    readme = (RECIPE_DIR / "README.md").read_text()
    for text in (
        *app.DOCUMENTED_BASE_URL_FORMS, app.API_KEY_ENV, app.BASE_URL_ENV, app.MODEL_ENV,
        "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS",
        "FI_HIDE_LLM_INVOCATION_PARAMETERS", "provider field says `openai`",
        "If the endpoint is not OpenAI-compatible, stop", "anyone with the URL",
        "treat the URL like a secret", "never commit it", "Append `/v1` yourself",
        "https://<workspace>-<endpoint>.xenon.lepton.run", "lep endpoint get -n <name>",
        "This recipe requires a non-empty `LEPTON_API_TOKEN`", "as NVIDIA recommends",
        "lower-cases the scheme and host (the path keeps its case)",
        "put `src/` on `PYTHONPATH` or run the snippet from `src/`",
        "HTTPS", "credentials", XENON_TEST_BASE_URL,
        "gen_ai.request.model", "endpoint URL and host are absent",
        "syntactic test host", "api.lepton.ai", "llm.lepton.run", "sdxl.lepton.run", "leptonai",
        "NVIDIA's Python SDK", "Non-OpenAI-compatible endpoints",
        'PYTHONPATH="python/examples/lepton/src:python:python/frameworks/openai:python/tests"',
        'PYTHONPATH="python/examples/lepton/src:python/tests"',
        "pytest python/examples/lepton/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs",
        "| 3.10, 3.11, 3.12, 3.13 | 3.24.0 |", "| 3.11 | 1.69.0 (the `traceai-openai` floor) |",
        "published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0`",
    ):
        assert text in readme
    assert "TBD" not in readme
    assert "*.lepton.run" not in readme
    assert "Do not guess `/v1`" not in readme
    assert "The OpenAI SDK requires a non-empty API key" not in readme
    assert "normalizes URL casing" not in readme
    assert "sk-" not in readme
    pins = (RECIPE_DIR / "requirements.txt").read_text().splitlines()
    assert pins[0].startswith("#")
    assert pins[1:] == ["openai==3.24.0", "traceAI-openai==0.1.10", "fi-instrumentation-otel==1.1.0"]
