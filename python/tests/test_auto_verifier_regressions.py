"""Regressions for the independent verifier's second-round agent findings."""

import asyncio
import functools
import gzip
import io
import json
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from auto_mock_server import CHAT_RESPONSE, MockServer, sse_body
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import _manager, _registry, wire
from fi_instrumentation.auto._config import parse_config_text, resolve
from fi_instrumentation.auto.wire import _core, _transports
from fi_instrumentation.auto.wire._capture import Recorder
from test_auto_manager import fresh_state  # noqa: F401 -- shared isolated manager fixture
from test_auto_safety import recording, only  # noqa: F401 -- shared recorder fixture

CHAT = {"model": "gpt", "messages": [{"role": "user", "content": "PRIVATE_INPUT"}]}
URL = "https://api.openai.com/v1/chat/completions"


def finish_chat(capture):
    assert capture is not None
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(json.dumps({"choices": [{"message": {
        "role": "assistant", "content": "PRIVATE_OUTPUT"
    }}]}).encode())
    capture.finish()


@pytest.mark.parametrize("flag", ["hide_input_messages", "hide_output_messages", "hide_input_images"])
def test_message_and_image_hide_flags_cover_raw_aliases(recording, flag):
    exporter, base = recording
    tracer = FITracer(base.__wrapped__, config=TraceConfig(**{flag: True}))
    body = {**CHAT, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "PRIVATE_INPUT"},
        {"type": "image_url", "image_url": {"url": "https://private.example/PRIVATE_IMAGE.png"}},
    ]}]}
    finish_chat(Recorder(tracer).begin("POST", URL, {}, json.dumps(body).encode()))
    attrs = only(exporter).attributes
    if flag == "hide_output_messages":
        assert attrs["output.value"] == "__REDACTED__"
        assert "PRIVATE_OUTPUT" not in str(dict(attrs))
    elif flag == "hide_input_messages":
        assert attrs["input.value"] == "__REDACTED__"
        assert "PRIVATE_INPUT" not in str(dict(attrs))
    else:
        assert "PRIVATE_IMAGE" not in str(dict(attrs))
        assert attrs["gen_ai.input.messages.0.message.content"] == "PRIVATE_INPUT"


@pytest.mark.parametrize("source", ["argument", "file"])
def test_explicit_empty_allowlist_disables_every_integration(tmp_path, source):
    kwargs = {"environ": {}}
    if source == "argument":
        kwargs["overrides"] = {"instrumentations": []}
    else:
        path = tmp_path / "empty.toml"
        path.write_text("instrumentations = []\n")
        kwargs["config_file"] = str(path)
    settings = resolve(**kwargs)
    assert settings.instrumentations == []
    assert not settings.wire_enabled()
    assert not settings.is_selected("openai")


@pytest.mark.parametrize("name", ["semantic_kernel", "strands", "agno", "beeai", "ag2_classic"])
def test_setup_shutdown_closes_exact_setup_handle(fresh_state, monkeypatch, name):
    calls = []

    class Handle:
        def instrument(self, **kwargs):
            calls.append(("enable", kwargs))

        def uninstrument(self):
            calls.append(("disable", {}))

    handle = Handle()

    def configure(*args, **kwargs):
        calls.append(("enable", kwargs))
        return handle

    module = SimpleNamespace(SemanticKernelInstrumentor=lambda: handle,
                             StrandsInstrumentor=lambda: handle,
                             configure_agno_tracing=configure, configure_beeai_tracing=configure,
                             setup=configure)
    integration = _registry.Integration(name, ("fakeailib",), "fake-setup", "traceai_setup_fake",
                                        name, kind=_registry.SETUP)
    monkeypatch.setitem(__import__("sys").modules, integration.module, module)
    monkeypatch.setattr(_registry, "ALL", (integration,))
    provider = TracerProvider(shutdown_on_exit=False)
    assert _manager.auto_instrument(tracer_provider=provider, lazy=False)[name] == _manager.INSTRUMENTED
    assert _manager.auto_instrument(disabled_instrumentations=[name])[name] == _manager.INSTRUMENTED
    assert [call[0] for call in calls] == ["enable"]
    _manager.shutdown()
    assert [call[0] for call in calls] == ["enable", "disable"]
    provider.shutdown()


@pytest.mark.parametrize("name", ["agent_framework", "livekit", "pipecat"])
def test_setup_shutdown_reverts_only_agent_processor_and_exporter_changes(fresh_state, monkeypatch, name):
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider(shutdown_on_exit=False)
    original, mapped, app_added = InMemorySpanExporter(), InMemorySpanExporter(), InMemorySpanExporter()
    route = SimpleSpanProcessor(original)
    provider.add_span_processor(route)
    mapper = SpanProcessor()

    def install(**kwargs):
        if name == "agent_framework":
            provider.add_span_processor(mapper)
        else:
            route.span_exporter = mapped
        return True

    integration = _registry.Integration(name, ("fakeailib",), "fake-setup", "traceai_setup_mapping",
                                        name, kind=_registry.SETUP)
    module = SimpleNamespace(enable_fi_attribute_mapping=install)
    monkeypatch.setitem(__import__("sys").modules, integration.module, module)
    monkeypatch.setattr(_registry, "ALL", (integration,))
    assert _manager.auto_instrument(tracer_provider=provider, lazy=False)[name] == _manager.INSTRUMENTED
    app_processor = SimpleSpanProcessor(app_added)
    provider.add_span_processor(app_processor)
    _manager.shutdown()
    assert provider._active_span_processor._span_processors == (route, app_processor)
    assert route.span_exporter is original
    provider.shutdown()


def test_agent_framework_setup_restores_native_enable_flag(fresh_state, monkeypatch):
    settings = SimpleNamespace(enable_instrumentation=False)
    monkeypatch.setitem(__import__("sys").modules, "agent_framework.observability",
                        SimpleNamespace(OBSERVABILITY_SETTINGS=settings))

    def enable(**kwargs):
        settings.enable_instrumentation = True
        return True

    result = _registry.SETUPS["agent_framework"](
        SimpleNamespace(enable_fi_attribute_mapping=enable), object(), object())
    assert settings.enable_instrumentation
    result.cleanup()
    assert not settings.enable_instrumentation


@pytest.mark.parametrize("app_owned", [False, True])
def test_explicit_grpc_transport_and_credentials_honored(fresh_state, monkeypatch, app_owned):
    from fi_instrumentation import otel

    if not otel._GRPC_INSTALLED:
        # The test environment deliberately installs only HTTP dependencies. A local
        # exporter stand-in still verifies transport selection on both provider paths.
        class GrpcExporter(InMemorySpanExporter):
            def __init__(self, endpoint=None, headers=None, timeout=None):
                super().__init__()
                self._endpoint = endpoint.removeprefix("http://")
                self._headers = headers

        monkeypatch.setattr(otel, "_GRPC_INSTALLED", True)
        monkeypatch.setattr(otel, "GRPCSpanExporter", GrpcExporter)
    if app_owned:
        trace.set_tracer_provider(TracerProvider(shutdown_on_exit=False))
    monkeypatch.setenv("FI_API_KEY", "environment-key")
    monkeypatch.setenv("FI_SECRET_KEY", "environment-secret")
    provider = _manager.init(api_key="explicit-key", secret_key="explicit-secret",
                             base_url="http://127.0.0.1:54321", transport=otel.Transport.GRPC,
                             headers={"X-API-Key": "header-key", "X-Secret-Key": "header-secret"}, instrumentations=[], batch=False)
    exporter = _manager._state.overlays[provider]._exporter
    assert isinstance(exporter, otel.GRPCSpanExporter)
    assert exporter._endpoint == "127.0.0.1:54321"
    assert dict(exporter._headers)["x-api-key"] == "header-key"
    assert dict(exporter._headers)["x-secret-key"] == "header-secret"
    _manager.shutdown()


def test_shutdown_queued_behind_unfinished_flush_is_not_lost(fresh_state, monkeypatch):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Provider:
        def force_flush(self, timeout_millis):
            calls.append("flush")
            entered.set()
            release.wait(2)
            return True

        def shutdown(self):
            calls.append("shutdown")
            stopped.set()

    monkeypatch.setattr(_manager, "_lifecycle_lock", threading.Lock())
    monkeypatch.setattr(_manager, "_shutdown_lock", threading.Lock())
    _manager._state.provider = Provider()
    _manager._state.owned_provider = True
    try:
        assert not _manager.flush(10)
        assert entered.is_set()
        start = time.monotonic()
        _manager.shutdown(10)
        _manager.shutdown(10)
        assert time.monotonic() - start < 0.5
        assert calls == ["flush"]
    finally:
        release.set()
    assert stopped.wait(1)
    _manager.shutdown(100)
    assert calls == ["flush", "shutdown"]
    assert not _manager._state.owned_provider


def test_overlay_shutdown_survives_unfinished_delegate_flush(fresh_state, monkeypatch):
    from opentelemetry.sdk.resources import Resource

    release, stopped = threading.Event(), threading.Event()

    class Processor:
        def force_flush(self, timeout_millis):
            release.wait(2)
            return True

        def shutdown(self):
            stopped.set()

    overlay = _manager._ResourceOverlayProcessor(Processor(), Resource({}))
    _manager._state.overlays[object()] = overlay
    monkeypatch.setattr(_manager, "_lifecycle_lock", threading.Lock())
    monkeypatch.setattr(_manager, "_shutdown_lock", threading.Lock())
    try:
        assert not overlay.force_flush(10)
        _manager.shutdown(10)
        assert not stopped.is_set()
    finally:
        release.set()
    assert stopped.wait(1)
    assert overlay._closed


@pytest.mark.parametrize("kind", ["http", "grpc"])
def test_positional_exporter_endpoint_and_headers_keep_precedence(monkeypatch, kind):
    from fi_instrumentation import otel

    if kind == "grpc" and not otel._GRPC_INSTALLED:
        pytest.skip("gRPC exporter not installed")
    monkeypatch.setenv("FI_BASE_URL", "http://environment.invalid")
    monkeypatch.setenv("FI_GRPC_URL", "http://environment.invalid:4317")
    monkeypatch.setenv("FI_API_KEY", "environment-key")
    monkeypatch.setenv("FI_SECRET_KEY", "environment-secret")
    cls = otel.HTTPSpanExporter if kind == "http" else otel.GRPCSpanExporter
    endpoint = "http://127.0.0.1:54321/custom"
    exporter = cls(endpoint, headers={"X-API-Key": "explicit-key", "X-Secret-Key": "explicit-secret"})
    try:
        assert exporter._endpoint == (endpoint if kind == "http" else "127.0.0.1:54321/custom")
        headers = getattr(exporter, "_headers", None) or exporter._client._headers
        assert dict(headers)["x-api-key"] == "explicit-key"
        assert dict(headers)["x-secret-key"] == "explicit-secret"
    finally:
        exporter.shutdown()


def test_unicode_toml_values_and_escapes_are_preserved(tmp_path):
    path = tmp_path / "unicode.toml"
    path.write_text('project_name = "café / 日本"\ninstrumentations = ["wire"]\n', encoding="utf-8")
    assert resolve(environ={}, config_file=str(path)).project_name == "café / 日本"
    assert parse_config_text(r'project_name = "café / 日本"')["project_name"] == "café / 日本"


@pytest.mark.parametrize("host,expected", [("api.deepgram.com", "deepgram"),
                                            ("api.openai.com", "openai"),
                                            ("bedrock-runtime.us-east-1.amazonaws.com", "aws.bedrock"),
                                            ("x.openai.azure.com", "azure.ai.openai"),
                                            ("unknown.example", "ollama")])
def test_provider_host_rules_beat_port_rules(host, expected):
    assert _core.resolve_provider(host, 11434) == expected
    if expected == "deepgram":
        assert _core.match("POST", f"http://{host}:11434/v1/listen") is not None
    assert _core.resolve_provider("bedrock-runtime.not-amazon.example", 443) is None


@pytest.mark.parametrize("encoding", ["custom-encoding", "gzip"])
def test_request_encoding_failure_or_expansion_keeps_required_marker(recording, encoding):
    exporter, tracer = recording
    recorder = Recorder(tracer, 128)
    body = b"opaque" if encoding == "custom-encoding" else gzip.compress(b"x" * 10000)
    capture = recorder.begin("POST", URL, {"content-encoding": encoding}, body)
    finish_chat(capture)
    attrs = only(exporter).attributes
    if encoding == "custom-encoding":
        assert attrs["traceai.wire.undecoded_encoding"] == encoding
        assert attrs["traceai.wire.body_unavailable"]
    else:
        assert attrs["traceai.wire.truncated"]
        assert "traceai.wire.body_unavailable" not in attrs
    assert "input.value" not in attrs


@pytest.mark.parametrize("direct_core", [False, True])
def test_gzip_request_is_decoded_before_validation(recording, direct_core):
    exporter, tracer = recording
    headers = {"content-type": "application/json", "content-encoding": "gzip"}
    body = gzip.compress(json.dumps(CHAT).encode())
    if direct_core:
        req = _core.RequestData("POST", URL, headers, body)
        result = _core.build(_core.match("POST", URL), req,
                             _core.ResponseData(status=200, headers={"content-type": "application/json"},
                                                body=json.dumps(CHAT_RESPONSE).encode()))
        assert result is not None
        attrs = result.attributes
    else:
        finish_chat(Recorder(tracer).begin("POST", URL, headers, body))
        attrs = only(exporter).attributes
    assert json.loads(attrs["input.value"]) == CHAT
    assert "traceai.wire.body_unavailable" not in attrs
    assert "traceai.wire.undecoded_encoding" not in attrs


@pytest.mark.parametrize("body", [{"input": "hello"}, {"model": "gpt", "prompt": {"id": "stored"}}])
def test_responses_accepts_either_input_or_model(recording, body):
    exporter, tracer = recording
    finish_chat(Recorder(tracer).begin("POST", "https://api.openai.com/v1/responses", {}, json.dumps(body).encode()))
    assert only(exporter).attributes["traceai.wire.endpoint"] == "openai.responses"


def test_responses_partial_stream_retains_tool_identity_and_arguments(recording):
    exporter, tracer = recording
    capture = Recorder(tracer).begin("POST", "https://api.openai.com/v1/responses", {},
                                     b'{"model":"gpt","input":"hello","stream":true}')
    capture.on_headers(200, {"content-type": "text/event-stream"})
    events = [
        {"type": "response.created", "response": {"id": "resp-1", "model": "gpt"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "fc-1", "type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc-1", "delta": '{"city":'},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '"Paris"}'},
    ]
    for event in events:
        capture.on_chunk(b"data: " + json.dumps(event).encode() + b"\n\n")
    capture.fail(TimeoutError("interrupted"))
    span = only(exporter)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes["error.type"] == "TimeoutError"
    base = "gen_ai.output.messages.0.message.tool_calls.0.tool_call"
    assert span.attributes[base + ".id"] == "call-1"
    assert span.attributes[base + ".function.name"] == "lookup"
    assert span.attributes[base + ".function.arguments"] == '{"city":"Paris"}'
    assert "Paris" in span.attributes["output.value"]


@pytest.mark.parametrize("version", [1, 2])
def test_cohere_stream_retains_completion_tools_and_usage(recording, version):
    exporter, tracer = recording
    capture = Recorder(tracer).begin("POST", f"https://api.cohere.com/v{version}/chat", {},
                                     json.dumps({"model": "command", "message": "hello", "stream": True}).encode())
    capture.on_headers(200, {"content-type": "text/event-stream"})
    if version == 2:
        events = [
            {"type": "message-start", "id": "cohere-1"},
            {"type": "content-delta", "delta": {"message": {"content": {"text": "hello "}}}},
            {"type": "content-delta", "delta": {"message": {"content": {"text": "world"}}}},
            {"type": "tool-call-start", "index": 0, "delta": {"message": {"tool_calls": {
                "id": "call-1", "function": {"name": "lookup", "arguments": "{"}}}}},
            {"type": "tool-call-delta", "index": 0, "delta": {"message": {"tool_calls": {
                "function": {"arguments": "}"}}}}},
            {"type": "message-end", "delta": {"finish_reason": "COMPLETE", "usage": {
                "tokens": {"input_tokens": 3, "output_tokens": 2}}}},
        ]
    else:
        events = [
            {"event_type": "stream-start", "generation_id": "cohere-1"},
            {"event_type": "text-generation", "text": "hello "},
            {"event_type": "text-generation", "text": "world"},
            {"event_type": "stream-end", "response": {"finish_reason": "COMPLETE", "meta": {
                "billed_units": {"input_tokens": 3, "output_tokens": 2}}}},
        ]
    for event in events:
        capture.on_chunk(b"data: " + json.dumps(event).encode() + b"\n\n")
    capture.finish()
    attrs = only(exporter).attributes
    assert attrs["output.value"] == "hello world"
    assert attrs["gen_ai.usage.input_tokens"] == 3
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.response.id"] == "cohere-1"
    if version == 2:
        base = "gen_ai.output.messages.0.message.tool_calls.0.tool_call"
        assert attrs[base + ".id"] == "call-1"
        assert attrs[base + ".function.arguments"] == "{}"


def test_gemini_message_parts_have_newline_separators(recording):
    exporter, tracer = recording
    parts = [{"text": "first"}, {"text": "second"}]
    capture = Recorder(tracer).begin("POST", "https://generativelanguage.googleapis.com/v1/models/gemini:generateContent",
                                     {}, json.dumps({"contents": [{"parts": parts}]}).encode())
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(json.dumps({"candidates": [{"content": {"role": "model", "parts": parts}}]}).encode())
    capture.finish()
    attrs = only(exporter).attributes
    assert attrs["gen_ai.input.messages.0.message.content"] == "first\nsecond"
    assert attrs["gen_ai.output.messages.0.message.content"] == "first\nsecond"
    assert attrs["output.value"] == "first\nsecond"


@pytest.mark.parametrize("body,expected", [
    ([1, {"data": "text"}], [1, {"data": "text"}]),
    ("hello", "hello"), (42, 42), (True, True), (None, None), ({}, {}),
])
def test_bedrock_invoke_accepts_any_json_body(recording, body, expected):
    exporter, tracer = recording
    raw = json.dumps(body).encode()
    capture = Recorder(tracer).begin("POST", "https://bedrock-runtime.us-east-1.amazonaws.com/model/model-id/invoke",
                                     {}, raw)
    assert capture is not None
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(b'{"ok":true}')
    capture.finish()
    # S6 preserves short unknown base64 without audio/MIME context or media magic.
    assert json.loads(only(exporter).attributes["input.value"]) == expected
    assert json.dumps(body).encode() == raw


@pytest.mark.parametrize("source", ["request", "headers", "ollama-default"])
def test_truncated_stream_keeps_streaming_attribute(recording, source):
    exporter, tracer = recording
    body = {**CHAT, "stream": True} if source == "request" else CHAT
    url = URL
    if source == "ollama-default":
        url = "http://localhost:11434/api/chat"
    capture = Recorder(tracer, 256).begin("POST", url, {}, json.dumps(body).encode())
    capture.on_headers(200, {"content-type": "text/event-stream" if source == "headers" else "application/json"})
    capture.on_chunk(b"x" * 512)
    capture.finish()
    attrs = only(exporter).attributes
    assert attrs["traceai.wire.truncated"]
    assert attrs["gen_ai.request.stream"] is True


def test_aiohttp_relative_base_url_resolves_catalog_provider(recording):
    aiohttp = pytest.importorskip("aiohttp")
    exporter, tracer = recording
    wire.enable(tracer, transports=["aiohttp"])

    class Resolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                     "proto": 0, "flags": 0}]

        async def close(self):
            pass

    async def run(port):
        connector = aiohttp.TCPConnector(resolver=Resolver())
        async with aiohttp.ClientSession(base_url=f"http://api.cohere.com:{port}", connector=connector) as session:
            async with session.post("/v2/chat", json={"message": "hello", "model": "command"}) as response:
                assert response.status == 404  # The local stand-in has no Cohere response route.
                await response.read()

    with MockServer() as server:
        asyncio.run(run(urlsplit(server.url).port))
    attrs = only(exporter).attributes
    assert attrs["gen_ai.provider.name"] == "cohere"
    assert attrs["traceai.wire.endpoint"] == "cohere.chat"
    assert attrs["server.address"] == "api.cohere.com"


@pytest.mark.parametrize("mode", ["read-all", "read-chunks", "readinto", "stream", "readall", "readline", "iterate", "data"])
def test_requests_raw_consumption_is_captured_without_duplicates(recording, mode):
    exporter, tracer = recording
    wire.enable(tracer, transports=["requests"])
    with MockServer() as server, requests.Session() as session:
        with session.post(server.url + "/v1/chat/completions", json=CHAT, stream=True) as response:
            assert response.raw.read(0) == b""
            assert not exporter.get_finished_spans()
            if mode == "read-all":
                data = response.raw.read()
            elif mode == "read-chunks":
                chunks = []
                while chunk := response.raw.read(13):
                    chunks.append(chunk)
                data = b"".join(chunks)
            elif mode == "readinto":
                chunks = []
                buffer = bytearray(13)
                while size := response.raw.readinto(buffer):
                    chunks.append(bytes(buffer[:size]))
                data = b"".join(chunks)
            elif mode == "stream":
                data = b"".join(response.raw.stream(13))
            elif mode == "readall":
                data = response.raw.readall()
            elif mode == "readline":
                data = response.raw.readline()
            elif mode == "iterate":
                data = b"".join(response.raw)
            else:
                data = response.raw.data
            assert json.loads(data) == CHAT_RESPONSE
    span = only(exporter)
    assert span.attributes["output.value"] == "pong"
    assert "traceai.wire.incomplete" not in span.attributes


def test_requests_raw_transport_failure_keeps_partial_output(recording):
    exporter, tracer = recording
    partial = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    class Raw:
        def __init__(self):
            self.first = True

        def read(self, amt=None):
            if self.first:
                self.first = False
                return partial
            raise requests.ReadTimeout("raw interrupted")

        def isclosed(self):
            return False

        def close(self):
            pass

    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(CHAT).encode())
    response = requests.Response()
    response.status_code = 200
    response.headers["content-type"] = "text/event-stream"
    response.raw = Raw()
    _transports._requests_response(capture, response, True)
    assert response.raw.read(100) == partial
    with pytest.raises(requests.ReadTimeout):
        response.raw.read(100)
    response.close()
    span = only(exporter)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes["error.type"] == "ReadTimeout"
    assert span.attributes["output.value"] == "partial"


@pytest.mark.parametrize("decoded", [False, True])
def test_requests_raw_compressed_reads_decode_exactly_once(recording, decoded):
    from urllib3.response import HTTPResponse

    exporter, tracer = recording
    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(CHAT).encode())
    data = gzip.compress(sse_body())
    response = requests.Response()
    response.status_code = 200
    response.headers.update({"content-type": "text/event-stream", "content-encoding": "gzip"})
    response.raw = HTTPResponse(body=io.BytesIO(data), headers=response.headers, preload_content=False,
                                decode_content=False)
    _transports._requests_response(capture, response, True)
    chunks = []
    while chunk := response.raw.read1(7, decode_content=decoded):
        chunks.append(chunk)
    assert b"".join(chunks) == (sse_body() if decoded else data)
    assert only(exporter).attributes["output.value"] == "pong"


@pytest.mark.parametrize("client", ["httpx", "httpx2", "requests"])
def test_disabling_preserves_application_outer_wrapper(recording, client):
    module = pytest.importorskip(client)
    exporter, tracer = recording
    owner = module.Session if client == "requests" else module.Client
    original = owner.send
    wire.enable(tracer, transports=[client])
    fi_send = owner.send
    calls = []

    @functools.wraps(fi_send)
    def application_send(self, *args, **kwargs):
        calls.append(True)
        return fi_send(self, *args, **kwargs)

    owner.send = application_send
    with MockServer() as server:
        with owner() as session:
            session.post(server.url + "/v1/chat/completions", json=CHAT)
            # Keep other transports active: the retained FI wrapper needs its own gate.
            wire.disable()
            assert owner.send is application_send
            session.post(server.url + "/v1/chat/completions", json=CHAT)
    assert calls == [True, True]
    assert len(exporter.get_finished_spans()) == 1
    wire.disable()
    owner.send = original


def test_disabling_preserves_aiohttp_application_outer_wrapper(recording):
    aiohttp = pytest.importorskip("aiohttp")
    exporter, tracer = recording
    original = aiohttp.ClientSession._request
    wire.enable(tracer, transports=["aiohttp"])
    fi_request = aiohttp.ClientSession._request
    calls = []

    @functools.wraps(fi_request)
    async def application_request(self, *args, **kwargs):
        calls.append(True)
        return await fi_request(self, *args, **kwargs)

    aiohttp.ClientSession._request = application_request

    async def run(url):
        async with aiohttp.ClientSession() as session:
            async with session.post(url + "/v1/chat/completions", json=CHAT) as response:
                await response.read()
            wire.disable()
            assert aiohttp.ClientSession._request is application_request
            async with session.post(url + "/v1/chat/completions", json=CHAT) as response:
                await response.read()

    with MockServer() as server:
        asyncio.run(run(server.url))
    assert calls == [True, True]
    assert len(exporter.get_finished_spans()) == 1
    wire.disable()
    aiohttp.ClientSession._request = original
