"""Round 6 pass A: A10 recognition parity with the spec oracle and transport arrival timing.

Loopback servers and synthetic payloads only.
"""

import ast
import asyncio
import importlib
import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from auto_fixture_loader import load_fixtures
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import wire
from fi_instrumentation.auto.wire import _core

SPEC = Path(__file__).resolve().parents[2] / "spec" / "auto-instrumentation"
_ORACLE_FUNCTIONS = ("nonempty", "tool_output", "tool_calls_output", "content_output", "first_output")


def _oracle() -> Any:
    """Load only the oracle's pure recognizer functions.

    generate_fixtures.py writes fixtures at import time, so it is never imported or run.
    """
    tree = ast.parse((SPEC / "generate_fixtures.py").read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in _ORACLE_FUNCTIONS]
    assert sorted(node.name for node in selected) == sorted(_ORACLE_FUNCTIONS)
    namespace: Dict[str, Any] = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "spec-oracle", "exec"), namespace)
    return namespace["first_output"]


FIRST_OUTPUT = _oracle()


def _fixture_events():
    """Every streamed event in every fixture: (fixture, SSE event header, payload)."""
    for fixture in load_fixtures():
        response = fixture["response"]
        for item in response.get("sse") or []:
            if isinstance(item, dict):
                item = dict(item)
                yield fixture["name"], item.pop("event", None), item
        for item in response.get("ndjson") or []:
            yield fixture["name"], None, item
        body = response.get("json")
        for item in body if isinstance(body, list) else [body] if isinstance(body, dict) else []:
            yield fixture["name"], None, item


def _mutations(event: Any):
    """Structural neighbours of real events: emptied, nulled and retyped payload fields."""
    yield event
    if not isinstance(event, dict):
        return
    for key, value in event.items():
        for replacement in ("", None, {}, [], " ", 0):
            if replacement != value:
                yield {**event, key: replacement}
        if isinstance(value, dict):
            for inner, inner_value in value.items():
                for replacement in ("", None, {}, "x"):
                    if replacement != inner_value:
                        yield {**event, key: {**value, inner: replacement}}


def _oracle_view(event: Any, header: Any) -> Any:
    # The oracle applies the SSE event header as event["event"] (overriding the JSON key).
    if isinstance(event, dict) and header is not None:
        return {**event, "event": header}
    return event


def _safe(function: Any, *args: Any) -> Any:
    try:
        return bool(function(*args))
    except (TypeError, AttributeError):
        return "malformed"


def test_output_recognizer_matches_spec_oracle_on_every_fixture_event():
    checked, positives, mismatches = 0, 0, []
    for name, header, event in _fixture_events():
        for candidate in _mutations(event):
            ours = _safe(_core._has_output_delta, candidate, header)
            oracle = _safe(FIRST_OUTPUT, _oracle_view(candidate, header))
            if "malformed" in (ours, oracle):
                # Malformed scaffolding is skipped by the replay loop, never output.
                continue
            checked += 1
            positives += oracle
            if ours != oracle:
                mismatches.append((name, header, candidate, ours, oracle))
    assert not mismatches[:10]
    assert checked > 5000 and positives > 300


@pytest.mark.parametrize("event", [
    {"event_type": "tool-calls-chunk", "text": "plan"},
    {"event_type": "tool-calls-chunk", "tool_call_delta": {"name": "lookup"}},
    {"event_type": "tool-calls-chunk", "tool_call_delta": {"parameters": "{\"city\":"}},
    {"event_type": "tool-calls-chunk", "tool_call_delta": {"parameters": {"city": "Oslo"}}},
    {"event_type": "tool-calls-chunk", "tool_call_delta": {"text": "Hé"}},
    {"type": "content-delta", "delta": {"message": {"content": [{"thinking": "t"}]}}},
    {"type": "content-start", "delta": {"message": {"content": {"type": "thinking", "thinking": "t"}}}},
    {"choices": [{"delta": {}}, {"delta": {"content": [{"type": "text", "text": ""},
                                                       {"type": "thinking", "thinking": [{"type": "text", "text": "t"}]}]}}]},
    {"type": "response.output_item.added", "item": {"type": "mcp_call", "name": "lookup"}},
    {"type": "response.mcp_call_arguments.delta", "delta": "{"},
    {"type": "response.output_item.added", "item": {"type": "code_interpreter_call", "code": "print(1)"}},
    {"type": "response.code_interpreter_call_code.delta", "delta": "print"},
    {"type": "response.output_item.added", "item": {"type": "custom_tool_call", "input": "x"}},
    {"type": "response.custom_tool_call_input.delta", "delta": "x"},
    {"type": "content_block_start", "content_block": {"type": "server_tool_use", "name": "web_search"}},
    {"type": "content_block_start", "content_block": {"type": "mcp_tool_use", "name": "lookup"}},
    {"candidates": [{"content": {"parts": [{"executableCode": {"language": "PYTHON", "code": "print(1)"}}]}}]},
])
def test_round6_output_forms_establish_first_output(event):
    assert _core._has_output_delta(event) is True


@pytest.mark.parametrize("event", [
    {"event_type": "tool-calls-chunk"},
    {"event_type": "tool-calls-chunk", "tool_call_delta": None},
    {"event_type": "tool-calls-chunk", "tool_call_delta": {}},
    {"event_type": "tool-calls-chunk", "text": "", "tool_call_delta": {"name": "", "parameters": {}, "text": None}},
    {"type": "content-start", "delta": {"message": {"content": {"type": "thinking"}}}},
    {"type": "citation-start", "delta": {"message": {"content": {"thinking": "metadata"}}}},
    {"choices": [{"delta": {"content": [{"type": "thinking", "thinking": [{"type": "citation", "text": "m"}]}]}}]},
    {"type": "response.output_item.added", "item": {"type": "mcp_call", "id": "mcp", "server_label": "catalog"}},
    {"type": "content_block_start", "content_block": {"type": "web_search_tool_result", "name": "web_search"}},
    {"candidates": [{"content": {"parts": [{"executableCode": {"language": "PYTHON"}}]}}]},
    {"candidates": [{"content": {"parts": [{"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "1"}}]}}]},
])
def test_round6_scaffolds_and_results_are_not_output(event):
    assert _core._has_output_delta(event) is False


# ---------------------------------------------------------------------------
# Transport boundary: per-event arrival timestamps from real clients.
# ---------------------------------------------------------------------------

PAUSE = 0.08
OPENAI_STREAM = [
    b'data: {"id":"c","model":"m","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
    b'data: {"id":"c","model":"m","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n",
]
# NDJSON framing applies to any catalog endpoint (an OpenAI-compatible server here: the
# Ollama provider is recognized by its default port, which a local Ollama may hold).
NDJSON_STREAM = [
    b'{"id":"c","model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n',
    b'{"id":"c","model":"m","choices":[{"index":0,"delta":{"content":"hello"}}]}\n'
    b'{"id":"c","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
]
GEMINI_STREAM = [
    b'[{"candidates":[{"content":{"role":"model","parts":[]}}]}',
    b',{"candidates":[{"content":{"role":"model","parts":[{"text":"hello"}]}}]}]',
]


class _TimedServer:
    """Writes the first frame, pauses, then writes the rest (chunked transfer)."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if self.path.startswith("/ndjson/"):
                    frames, content_type = NDJSON_STREAM, "application/x-ndjson"
                elif self.path.endswith(":streamGenerateContent"):
                    frames, content_type = GEMINI_STREAM, "application/json"
                else:
                    frames, content_type = OPENAI_STREAM, "text/event-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for index, frame in enumerate(frames):
                    if index:
                        time.sleep(PAUSE)
                    self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_TimedServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def timed():
    with _TimedServer() as server:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        wire.enable(FITracer(provider.get_tracer("timed"), config=TraceConfig()))
        try:
            yield server, exporter
        finally:
            wire.disable()


ROUTES = {
    "sse": ("/v1/chat/completions", {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
    "ndjson": ("/ndjson/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
    "json": ("/v1beta/models/m:streamGenerateContent", {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}),
}


def _call(client: str, url: str, body: Dict[str, Any]) -> bytes:
    if client == "requests":
        import requests

        with requests.Session() as session:
            response = session.post(url, json=body, stream=True)
            return b"".join(response.iter_content(chunk_size=None))
    if client in ("httpx", "httpx2"):
        module = importlib.import_module(client)
        with module.Client() as session:
            with session.stream("POST", url, json=body) as response:
                return b"".join(response.iter_bytes())
    if client == "httpx-async":
        import httpx

        async def run() -> bytes:
            async with httpx.AsyncClient() as session:
                async with session.stream("POST", url, json=body) as response:
                    return b"".join([chunk async for chunk in response.aiter_bytes()])
        return asyncio.run(run())
    import aiohttp

    async def run_aiohttp() -> bytes:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body) as response:
                return b"".join([chunk async for chunk in response.content.iter_any()])
    return asyncio.run(run_aiohttp())


CLIENTS = ["requests", "httpx", "httpx-async"] + [
    name for name in ("httpx2", "aiohttp") if importlib.util.find_spec(name)]


@pytest.mark.parametrize("framing", sorted(ROUTES))
@pytest.mark.parametrize("client", CLIENTS)
def test_transport_records_first_chunk_and_first_text_from_arrivals(timed, client, framing):
    server, exporter = timed
    path, body = ROUTES[framing]
    received = _call(client, server.url + path, body)
    expected = {"sse": OPENAI_STREAM, "ndjson": NDJSON_STREAM, "json": GEMINI_STREAM}[framing]
    assert received == b"".join(expected)  # the application sees exactly the server's bytes
    (span,) = exporter.get_finished_spans()
    attrs = span.attributes
    first_chunk = attrs["gen_ai.response.time_to_first_chunk"]
    first_text = attrs["gen_ai.response.time_to_first_text"]
    # The role-only/empty first frame is a chunk, not output; text arrives after the pause.
    assert first_text - first_chunk >= PAUSE * 0.75, (first_chunk, first_text)
    duration = (span.end_time - span.start_time) / 1e9
    assert first_chunk < first_text <= duration + 1e-3
    if framing != "ndjson":  # the chat parser reads SSE/JSON bodies; timing covers all three
        assert attrs["output.value"] == "hello"


# ---------------------------------------------------------------------------
# A1: one resolved configuration drives default (instrumentor) policies too.
# ---------------------------------------------------------------------------

from dataclasses import fields as _fields  # noqa: E402

from fi_instrumentation import _configuration, register  # noqa: E402
from fi_instrumentation.auto import _manager  # noqa: E402
from fi_instrumentation.auto._config import resolve  # noqa: E402
from test_auto_manager import fresh_state  # noqa: E402,F401

_PRIVACY_ENV = [f.metadata["env_var"] for f in _fields(TraceConfig)]


@pytest.fixture
def privacy_env(fresh_state, monkeypatch):
    for name in _PRIVACY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_configuration, "_warned", set())
    return fresh_state


def _instrumentor_default_policy(**kwargs: Any) -> TraceConfig:
    # What 56 traceAI instrumentors do when the application passes no config.
    return kwargs.get("config") or TraceConfig()


def _export_one(policy: TraceConfig, **attributes: Any):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("manual"), config=policy)
    with tracer.start_as_current_span("llm", attributes={"gen_ai.span.kind": "LLM", **attributes}):
        pass
    provider.shutdown()
    (span,) = exporter.get_finished_spans()
    return span


@pytest.mark.parametrize("source", ["implicit", "FI_CONFIG_FILE"])
def test_default_instrumentor_policy_applies_file_hiding(privacy_env, monkeypatch, source):
    # s6 final #1: the env-only TraceConfig() used to ignore futureagi.toml hiding.
    path = privacy_env / ("futureagi.toml" if source == "implicit" else "custom.toml")
    path.write_text("hide_inputs = true\nhide_outputs = true\n")
    if source != "implicit":
        monkeypatch.setenv("FI_CONFIG_FILE", str(path))
    policy = _instrumentor_default_policy()
    assert policy.hide_inputs and policy.hide_outputs and policy.hide_error_details
    span = _export_one(policy, **{"input.value": "PRIVATE_INPUT", "output.value": "PRIVATE_OUTPUT"})
    assert span.attributes["input.value"] == "__REDACTED__"
    assert span.attributes["output.value"] == "__REDACTED__"
    assert FITracer(TracerProvider().get_tracer("t"))._self_config.hide_inputs


def test_all_twelve_fields_resolve_identically_on_manual_and_agent_paths(privacy_env):
    values = {"base64_image_max_length": 17}
    lines = [f"{f.name.lstrip('_')} = {json.dumps(values.get(f.name.lstrip('_'), True))}"
             for f in _fields(TraceConfig)]
    (privacy_env / "futureagi.toml").write_text("\n".join(lines) + "\n")
    manual = TraceConfig()
    agent = resolve(environ={}).trace_config
    assert len(lines) == 12
    for f in _fields(TraceConfig):
        assert getattr(manual, f.name) == getattr(agent, f.name), f.name


@pytest.mark.parametrize("spelling,expected", [
    ("1", True), ("yes", True), ("ON", True), ("true", True),
    ("0", False), ("no", False), ("off", False), ("False", False),
])
def test_environment_spellings_mean_the_same_on_every_path(privacy_env, monkeypatch, spelling, expected):
    monkeypatch.setenv("FI_HIDE_INPUTS", spelling)
    assert TraceConfig().hide_inputs is expected
    assert resolve().trace_config.hide_inputs is expected


def test_precedence_code_then_environment_then_file(privacy_env, monkeypatch):
    (privacy_env / "futureagi.toml").write_text("hide_inputs = true\nhide_outputs = true\n")
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "false")
    policy = TraceConfig()
    assert policy.hide_inputs is True and policy.hide_outputs is False
    explicit = TraceConfig(hide_inputs=False)
    assert explicit.hide_inputs is False and explicit.hide_outputs is False


def test_agent_pins_one_policy_for_later_default_policies(privacy_env):
    # init(config_file=...) is the process configuration; manual instrumentors follow it.
    custom = privacy_env / "elsewhere.toml"
    custom.write_text("hide_outputs = true\nbase64_image_max_length = 11\n")
    exporter = InMemorySpanExporter()
    _manager.init(config_file=str(custom), span_exporter=exporter, batch=False, instrumentations=[])
    pinned = _manager._state.trace_config
    later = _instrumentor_default_policy()
    assert later.hide_outputs is True and later.base64_image_max_length == 11
    assert all(getattr(later, f.name) == getattr(pinned, f.name) for f in _fields(TraceConfig))
    assert TraceConfig(hide_outputs=False).hide_outputs is False
    _manager.shutdown()


@pytest.mark.parametrize("text", ["hide_inputs = true\nthis is not toml\n", "hide_outputs = yes\n"])
def test_unreadable_file_fails_closed_on_manual_paths_without_raising(privacy_env, text):
    import logging

    (privacy_env / "futureagi.toml").write_text(text + 'api_key = "fi-secret-value-123456"\n')
    records: List[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger("fi_instrumentation.auto")
    log.addHandler(handler)
    try:
        policy = TraceConfig()
        tracer = FITracer(TracerProvider().get_tracer("t"))
        TraceConfig()  # warned once, not per policy
    finally:
        log.removeHandler(handler)
    assert policy.hide_inputs and policy.hide_outputs and policy.hide_llm_invocation_parameters
    assert tracer._self_config.hide_inputs
    assert len(records) == 1
    messages = " ".join(record.getMessage() for record in records)
    assert "futureagi.toml" in messages and "yes" not in messages and "fi-secret" not in messages
    # The agent path stays strict: it refuses to start rather than guess.
    with pytest.raises(_configuration.ConfigError):
        resolve()


def test_missing_named_config_file_fails_closed(privacy_env, monkeypatch):
    monkeypatch.setenv("FI_CONFIG_FILE", str(privacy_env / "absent.toml"))
    assert TraceConfig().hide_inputs is True
    monkeypatch.setenv("FI_HIDE_INPUTS", "false")  # an explicit higher source still wins
    assert TraceConfig().hide_inputs is False


def test_invalid_scalar_keeps_long_standing_default_and_cache_tracks_edits(privacy_env):
    path = privacy_env / "futureagi.toml"
    path.write_text("hide_inputs = 5\n")
    assert TraceConfig().hide_inputs is False
    path.write_text("hide_inputs = true # edited\n")
    assert TraceConfig().hide_inputs is True


# ---------------------------------------------------------------------------
# S1: every traceAI-owned export route, without the agent too.
# ---------------------------------------------------------------------------

SECRET = "fi-secret-r6a-0123456789"
STRUCTURAL = "STRUCTURAL_SECRET_123456"


def _native_span(provider: Any) -> None:
    # An SDK-native/application span: no FITracer, so only the export boundary protects it.
    with provider.get_tracer("native").start_as_current_span("native", attributes={
            "input.value": "PRIVATE_INPUT", "output.value": "PRIVATE_OUTPUT", "api_key": STRUCTURAL,
            "note": f"echo {SECRET}", "image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAE="}):
        pass


def _assert_clean(attributes: Mapping) -> None:
    flat = json.dumps({key: list(value) if isinstance(value, tuple) else value
                       for key, value in dict(attributes).items()})
    assert "PRIVATE_INPUT" not in flat and "PRIVATE_OUTPUT" not in flat
    assert STRUCTURAL not in flat and SECRET not in flat and "iVBORw0KGgo" not in flat


@pytest.fixture
def manual_secrets(privacy_env, monkeypatch):
    monkeypatch.setenv("FI_API_KEY", "fi-api-r6a-0123456789")
    monkeypatch.setenv("FI_SECRET_KEY", SECRET)
    (privacy_env / "futureagi.toml").write_text("hide_inputs = true\nhide_outputs = true\n")
    return privacy_env


def test_manual_register_exporter_route_is_sanitized_and_host_view_untouched(manual_secrets):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor as OTelSimple

    exporter = InMemorySpanExporter()
    provider = register(project_name="p", span_exporter=exporter, batch=False,
                        verbose=False, signal_handlers=False)
    host = InMemorySpanExporter()
    provider.add_span_processor(OTelSimple(host))  # application-owned destination
    _native_span(provider)
    provider.shutdown()
    (span,) = exporter.get_finished_spans()
    _assert_clean(span.attributes)
    assert span.attributes["input.value"] == "__REDACTED__"
    assert span.name == "native" and span.context.span_id == host.get_finished_spans()[0].context.span_id
    # S8: host-owned exporters still see the application's original view.
    assert host.get_finished_spans()[0].attributes["input.value"] == "PRIVATE_INPUT"


class _Collector:
    def __init__(self) -> None:
        self.bodies: List[bytes] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                owner.bodies.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def spans(self) -> List[Any]:
        import gzip

        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        out = []
        for body in self.bodies:
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            out.extend(request.resource_spans)
        return out

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("route", ["register", "provider-default", "upstream-processor"])
def test_owned_http_exporter_routes_are_sanitized_on_the_wire(manual_secrets, monkeypatch, route):
    from google.protobuf.json_format import MessageToJson
    from opentelemetry.sdk.trace import TracerProvider as SdkProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor as OTelSimple

    from fi_instrumentation.otel import HTTPSpanExporter, TracerProvider as FiProvider

    collector = _Collector()
    monkeypatch.setenv("FI_BASE_URL", collector.url)
    try:
        if route == "register":
            provider = register(project_name="p", batch=False, verbose=False, signal_handlers=False)
        elif route == "provider-default":
            provider = FiProvider(verbose=False)
        else:
            provider = SdkProvider()
            provider.add_span_processor(OTelSimple(HTTPSpanExporter()))
        _native_span(provider)
        provider.force_flush()
        exported = "".join(MessageToJson(resource) for resource in collector.spans())
    finally:
        collector.close()
    assert '"native"' in exported
    assert "PRIVATE_INPUT" not in exported and "PRIVATE_OUTPUT" not in exported
    assert STRUCTURAL not in exported and SECRET not in exported and "iVBORw0KGgo" not in exported


def test_grpc_exporter_sanitizes_its_batches(manual_secrets, monkeypatch):
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    from fi_instrumentation.otel import GRPCSpanExporter

    seen: List[Any] = []
    monkeypatch.setattr(OTLPSpanExporter, "export", lambda self, spans, *a, **k: seen.extend(spans))
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(GRPCSpanExporter(endpoint="http://127.0.0.1:9")))
    _native_span(provider)
    (span,) = seen
    _assert_clean(span.attributes)


@pytest.mark.parametrize("outer", [False, True])
def test_outer_boundary_sanitizes_each_span_once(manual_secrets, monkeypatch, outer):
    from fi_instrumentation._export_privacy import PrivacyExporter
    from fi_instrumentation.auto.wire import _privacy
    from fi_instrumentation.otel import HTTPSpanExporter

    calls: List[str] = []
    original = _privacy.export_span
    monkeypatch.setattr(_privacy, "export_span", lambda span, *a, **k: calls.append(span.name) or original(span, *a, **k))
    sent: List[Any] = []
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter.export",
                        lambda self, spans: sent.extend(spans))
    owned = HTTPSpanExporter()
    provider = register(project_name="p", span_exporter=PrivacyExporter(owned) if outer else owned,
                        batch=False, verbose=False, signal_handlers=False)
    _native_span(provider)
    provider.shutdown()
    assert calls == ["native"]
    (span,) = sent
    _assert_clean(span.attributes)


def test_manual_route_differential_application_behaviour(manual_secrets):
    # Agent-on vs agent-off for the application: same return values and exceptions.
    def application(provider: Any) -> Any:
        tracer = provider.get_tracer("app")
        with tracer.start_as_current_span("work") as span:
            span.set_attribute("input.value", "PRIVATE_INPUT")
            result = {"answer": 42}
        try:
            with tracer.start_as_current_span("fails"):
                raise KeyError("boom")
        except KeyError as error:
            return result, repr(error), span.attributes["input.value"]

    baseline = application(TracerProvider())  # plain OpenTelemetry SDK, no traceAI route
    exporter = InMemorySpanExporter()
    provider = register(project_name="p", span_exporter=exporter, batch=False, verbose=False, signal_handlers=False)
    observed = application(provider)
    provider.shutdown()
    assert observed[:2] == baseline[:2]
    assert observed[2] == "PRIVATE_INPUT"  # the live span the application holds is never rewritten
    assert {span.name for span in exporter.get_finished_spans()} == {"work", "fails"}


# ---------------------------------------------------------------------------
# A7: deterministic projection (spec rules 1-4).
# ---------------------------------------------------------------------------

import random  # noqa: E402

from fi_instrumentation._attribute_limits import (  # noqa: E402
    DROPPED_CONTENT_COUNT, PROTECTED_FAMILIES, project_attributes)

PROTECTED = {
    "gen_ai.span.kind": "LLM", "gen_ai.response.id": "r1", "error.type": "500",
    "http.response.status_code": 500, "gen_ai.response.finish_reasons": ("stop",),
    "gen_ai.provider.name": "openai", "gen_ai.request.model": "m", "gen_ai.operation.name": "chat",
    "gen_ai.usage.input_tokens": 5, "gen_ai.usage.output_tokens": 1, "gen_ai.usage.total_tokens": 6,
    "traceai.capture.layer": "wire", "traceai.wire.endpoint": "openai.chat_completions",
    "http.request.method": "POST", "traceai.wire.incomplete": True,
}


def _chat(messages: int) -> Dict[str, Any]:
    attrs = dict(PROTECTED)
    for i in range(messages):
        attrs[f"gen_ai.input.messages.{i}.message.role"] = "user"
        attrs[f"gen_ai.input.messages.{i}.message.content"] = f"m{i}"
    attrs.update({
        "gen_ai.output.messages.0.message.role": "assistant",
        "gen_ai.output.messages.0.message.content": "answer",
        "output.value": "answer", "output.mime_type": "text/plain",
        "input.value": "{...}", "input.mime_type": "application/json",
        "gen_ai.tool.definitions.0.tool.json_schema": "{}",
        "gen_ai.request.temperature": 0.2,
    })
    return attrs


@pytest.mark.parametrize("limit", [128, 64, 32, 24, 20])
def test_projection_reserves_semantics_and_keeps_newest_contiguous_input(limit):
    candidates = _chat(100)
    selected, omitted = project_attributes(candidates, limit, "chat")
    assert len(selected) <= limit
    assert all(selected[key] == value for key, value in PROTECTED.items())
    assert selected["output.value"] == "answer" and selected["output.mime_type"] == "text/plain"
    assert selected["gen_ai.output.messages.0.message.content"] == "answer"
    indices = sorted({int(key.split(".")[3]) for key in selected if key.startswith("gen_ai.input.messages.")})
    if indices:
        assert indices == list(range(indices[0], 100))  # oldest input dropped first, no gaps
        for i in indices:  # atomic groups: never a dangling role or content
            assert f"gen_ai.input.messages.{i}.message.role" in selected
            assert f"gen_ai.input.messages.{i}.message.content" in selected
    assert "input.value" not in selected and "input.mime_type" not in selected
    content = [key for key in candidates if key.startswith(("gen_ai.input.", "gen_ai.output.", "input.",
                                                            "output.", "gen_ai.tool."))]
    emitted = [key for key in selected if key in content]
    assert selected[DROPPED_CONTENT_COUNT] == len(content) - len(emitted)
    assert omitted == len(candidates) - (len(selected) - 1)


def test_projection_is_independent_of_insertion_order():
    candidates = list(_chat(60).items())
    expected = project_attributes(dict(candidates), 48, "chat")
    for seed in range(5):
        random.Random(seed).shuffle(candidates)
        assert project_attributes(dict(candidates), 48, "chat") == expected


def test_mime_companion_never_separated_from_payload():
    attrs = {**PROTECTED, "output.value": "x" * 10, "output.mime_type": "text/plain",
             "input.value": "{}", "input.mime_type": "application/json"}
    room = len(PROTECTED) + 1 + 1  # protected, count marker, one more slot
    selected, _ = project_attributes(attrs, room, "chat")
    assert ("output.value" in selected) == ("output.mime_type" in selected)
    assert ("input.value" in selected) == ("input.mime_type" in selected)
    assert selected[DROPPED_CONTENT_COUNT] == 4


@pytest.mark.parametrize("limit", [0, 1, 3, 8])
def test_protected_overflow_admits_no_content_in_family_order(limit):
    selected, _ = project_attributes(_chat(3), limit, "chat")
    assert len(selected) == limit
    ordered = [DROPPED_CONTENT_COUNT] + [key for family in PROTECTED_FAMILIES
                                         for key in (sorted(family) if family else
                                                     sorted(k for k in PROTECTED if k.startswith("gen_ai.usage.")))
                                         if key in PROTECTED or key == DROPPED_CONTENT_COUNT]
    ordered = list(dict.fromkeys(ordered))
    assert set(selected) == set(ordered[:limit])
    assert not any(key.startswith(("input.", "output.", "gen_ai.input.", "gen_ai.output.")) for key in selected)


def test_output_wins_and_input_waits_while_output_is_omitted():
    attrs = {**PROTECTED, "gen_ai.output.messages.0.message.content": "o" ,
             "gen_ai.output.messages.0.message.role": "assistant",
             "gen_ai.output.messages.1.message.content": "o2",
             "gen_ai.output.messages.1.message.role": "assistant",
             "gen_ai.input.messages.0.message.content": "i"}
    selected, _ = project_attributes(attrs, len(PROTECTED) + 3, "chat")
    assert "gen_ai.output.messages.0.message.content" in selected
    assert "gen_ai.output.messages.1.message.content" not in selected
    assert "gen_ai.input.messages.0.message.content" not in selected  # would fit, but output is omitted


def test_existing_count_accumulates_and_fitting_spans_are_untouched():
    fitting = _chat(2)
    assert project_attributes(fitting, 128, "chat") == (fitting, 0)
    selected, _ = project_attributes({**_chat(50), DROPPED_CONTENT_COUNT: 7}, 40, "chat")
    fresh, _ = project_attributes(_chat(50), 40, "chat")
    assert selected[DROPPED_CONTENT_COUNT] == fresh[DROPPED_CONTENT_COUNT] + 7


def test_library_span_projection_uses_spec_markers_and_survives_sanitization():
    from opentelemetry.sdk.trace import SpanLimits

    from fi_instrumentation.auto.wire._privacy import export_span

    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=40))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("library"), config=TraceConfig())
    with tracer.start_as_current_span("chat", attributes={"gen_ai.span.kind": "LLM",
                                                          "gen_ai.provider.name": "openai"}) as span:
        for key, value in _chat(0).items():
            span.set_attribute(key, value)
        for i in range(30):
            span.set_attribute(f"gen_ai.input.messages.{i}.message.content", "content")
    (ended,) = exporter.get_finished_spans()
    assert len(ended.attributes) <= 40 and ended.dropped_attributes > 0
    assert ended.attributes["gen_ai.usage.input_tokens"] == 5
    assert "traceai.attributes.truncated" not in ended.attributes
    again = export_span(export_span(ended, TraceConfig()), TraceConfig())
    assert again.dropped_attributes == ended.dropped_attributes
    assert set(again.attributes) == set(ended.attributes)
    provider.shutdown()


@pytest.mark.parametrize("loss", ["encoded", "timing_truncated", None])
def test_lost_arrival_association_omits_first_text_and_marks_incomplete(loss):
    import gzip

    body = b"".join(OPENAI_STREAM)
    request = _core.RequestData("POST", "https://api.openai.com/v1/chat/completions",
                                {"content-type": "application/json"},
                                json.dumps(ROUTES["sse"][1]).encode())
    headers = {"content-type": "text/event-stream"}
    if loss == "encoded":
        body, headers = gzip.compress(body), {**headers, "content-encoding": "gzip"}
    response = _core.ResponseData(status=200, headers=headers, body=body, size=len(body))
    response.chunk_arrivals = [(len(body) // 2, 0.01), (len(body), 0.05)]
    response.first_chunk_seconds = 0.01
    response.timing_truncated = loss == "timing_truncated"
    if loss == "timing_truncated":
        response.chunk_arrivals = response.chunk_arrivals[:1]
    result = _core.build(_core.match(request.method, request.url), request, response)
    assert result.attributes["output.value"] == "hello"
    if loss is None:
        assert result.attributes["gen_ai.response.time_to_first_text"] == 0.05
        assert "traceai.wire.incomplete" not in result.attributes
    else:
        assert "gen_ai.response.time_to_first_text" not in result.attributes
        assert result.attributes["traceai.wire.incomplete"] is True
