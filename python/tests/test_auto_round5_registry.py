"""S5: dispatch observation is independent of wire capture and trace ancestry.

All credentials are synthetic. Every network request stays on loopback.
"""

import asyncio
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from fi_instrumentation import FITracer, TraceConfig, _secrets, register_secret
from fi_instrumentation.auto import _manager, wire
from fi_instrumentation.auto.wire import _privacy
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_round4_privacy_boundary import snapshot
from test_auto_safety import CHAT


@contextmanager
def echo_server(digest=False, credential_header=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            authorization = self.headers.get("Authorization", "")
            status = 401 if digest and not authorization.startswith("Digest ") else 200
            if self.path.startswith("/redirect/"):
                status = 307
            credentials = self.headers.get_all(credential_header, []) if credential_header else [authorization]
            text = " ".join(credentials) + " " + self.path
            body = json.dumps({"model": "test", "choices": [{"message": {"content": text}}]}).encode()
            self.send_response(status)
            if status == 401:
                self.send_header("WWW-Authenticate", 'Digest realm="test", nonce="fixed-nonce-1234", qop="auth"')
            if status == 307:
                self.send_header("Location", "/v1/chat/completions?token=REDIRECT_QUERY_SECRET_1234")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def install_agent():
    app, fi = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(app))
    _manager.init(tracer_provider=provider, span_exporter=fi, batch=False, instrumentations=["wire"])
    return provider, app, fi


def assert_process_exports(provider, exporters, forbidden):
    # This span has no parent, starts after the request's ancestors ended, and can
    # run on another thread. No trace grouping/inheritance may be needed to mask it.
    def later_trace():
        with provider.get_tracer("application").start_as_current_span("unrelated trace") as span:
            span.set_attribute("output.value", " ".join(forbidden))
            span.add_event("echo", {"exception.message": " ".join(forbidden)})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(later_trace).result(5)
    for exporter in exporters:
        spans = exporter.get_finished_spans()
        assert spans[-1].name == "unrelated trace"
        content = repr([snapshot(span) for span in spans])
        assert all(value not in content for value in forbidden)


@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
@pytest.mark.parametrize("hook_point", ["on_request_start", "on_request_headers_sent"])
def test_aiohttp_late_headers_and_query_differential(fresh_state, kind, hook_point):
    aiohttp = pytest.importorskip("aiohttp")
    secret, query = "LATE_AIOHTTP_PROVIDER_KEY_123456", "LATE_AIOHTTP_QUERY_KEY_123456"

    async def run(url):
        hooks = aiohttp.TraceConfig()
        async def hook(session, context, params):
            params.headers["Authorization"] = "Bearer " + secret
        getattr(hooks, hook_point).append(hook)
        async with aiohttp.ClientSession(trace_configs=[hooks]) as client:
            async with client.post(url, json=CHAT) as response:
                return response.status, await response.read(), [r.status for r in response.history]

    with echo_server() as server:
        url = server + "/v1/chat/completions?api_key=" + query
        baseline = asyncio.run(run(url))
        provider, app, fi = install_agent()
        with FITracer(provider.get_tracer("library")).start_as_current_span(
                "outer-" + kind, attributes={"gen_ai.span.kind": kind}) as outer:
            with provider.get_tracer("native").start_as_current_span("anthropic.messages.create") as native:
                actual = asyncio.run(run(url))
                native.set_attribute("output.value", actual[1].decode())
            outer.set_attribute("output.value", actual[1].decode())
        assert actual == baseline
        assert secret.encode() in actual[1]
    assert_process_exports(provider, (app, fi), [secret, query])
    for exporter in (app, fi):
        spans = exporter.get_finished_spans()
        assert len(spans) == (3 if kind == "LLM" else 4)
        assert sum(s.attributes.get("traceai.capture.layer") == "wire" for s in spans) == (kind == "CHAIN")
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
@pytest.mark.parametrize("streamed", [False, True])
def test_requests_digest_adapter_retry_differential(fresh_state, monkeypatch, kind, streamed):
    import requests
    # Requests' Digest cnonce is random/time based; deterministic inputs let us
    # compare the full response bytes, including the echoed Authorization value.
    monkeypatch.setattr(requests.auth.os, "urandom", lambda count: b"x" * count)
    monkeypatch.setattr(requests.auth.time, "ctime", lambda *args: "fixed test clock")

    def run(url):
        with requests.Session() as client:
            response = client.post(url, json=CHAT, auth=requests.auth.HTTPDigestAuth(
                "PROVIDER_USERNAME", "SYNTHETIC_PASSWORD_1234"), stream=streamed)
            content = response.content
            return response.status_code, content, [r.status_code for r in response.history], response.request.headers["Authorization"]

    with echo_server(digest=True) as server:
        url = server + "/v1/chat/completions"
        baseline = run(url)
        provider, app, fi = install_agent()
        with FITracer(provider.get_tracer("library")).start_as_current_span(
                "outer-" + kind, attributes={"gen_ai.span.kind": kind}) as outer:
            actual = run(url)
            outer.set_attribute("output.value", actual[1].decode())
        assert actual == baseline
        assert actual[0] == 200 and actual[2] == [401]
        assert actual[3] in json.loads(actual[1])["choices"][0]["message"]["content"]
    assert_process_exports(provider, (app, fi), [actual[3], actual[3].split(" ", 1)[1]])
    for exporter in (app, fi):
        spans = exporter.get_finished_spans()
        assert len(spans) == (2 if kind == "LLM" else 3)
        captured = [s for s in spans if s.attributes.get("traceai.capture.layer") == "wire"]
        if kind == "CHAIN":
            assert len(captured) == 1
            assert captured[0].attributes["http.response.status_code"] == 200
            assert captured[0].status.status_code == trace.StatusCode.OK
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
def test_httpx_final_transport_headers_url_differential(fresh_state, module, asynchronous, kind):
    httpx = importlib.import_module(module)
    secret, query = "HTTPX_FINAL_AUTH_12345678", "HTTPX_FINAL_QUERY_12345678"
    responses = []

    class Auth(httpx.Auth):
        def auth_flow(self, request):
            request.headers["Authorization"] = "Bearer " + secret
            request.url = request.url.copy_add_param("token", query)
            yield request

    def handle(request):
        response = httpx.Response(200, json={"choices": [{"message": {"content": str(request.url) + " " + request.headers["Authorization"]}}]})
        responses.append(response)
        return response

    async def async_handle(request):
        return handle(request)

    def run():
        if asynchronous:
            async def send():
                async with httpx.AsyncClient(transport=httpx.MockTransport(async_handle), auth=Auth()) as client:
                    return await client.post("http://127.0.0.1/v1/chat/completions", json=CHAT)
            response = asyncio.run(send())
        else:
            with httpx.Client(transport=httpx.MockTransport(handle), auth=Auth()) as client:
                response = client.post("http://127.0.0.1/v1/chat/completions", json=CHAT)
        assert response is responses[-1]
        return response.status_code, response.content

    baseline = run()
    provider, app, fi = install_agent()
    with FITracer(provider.get_tracer("library")).start_as_current_span(
            "outer-" + kind, attributes={"gen_ai.span.kind": kind}) as outer:
        actual = run()
        outer.set_attribute("output.value", actual[1].decode())
    assert actual == baseline
    assert secret.encode() in actual[1]
    assert_process_exports(provider, (app, fi), [secret, query])
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
@pytest.mark.parametrize("header", ["X-API-Key", "Authorization"])
def test_httpx_duplicate_credentials_observed_without_splitting_values(monkeypatch, module, header):
    httpx = importlib.import_module(module)
    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())
    values = ["Bearer DUPLICATE_CREDENTIAL_A_12345678,comma-part",
              "Bearer DUPLICATE_CREDENTIAL_B_12345678"]
    headers = httpx.Headers([(header, values[0]), (header.lower(), values[1])])
    original = headers.raw
    _privacy.observe_request(headers)
    registered = _secrets.snapshot()
    assert all(value in registered for value in values)
    assert headers[header] not in registered  # Combined .items() value was never sent.
    assert values[0].split(",")[0] not in registered  # Never split arbitrary credentials.
    if header == "Authorization":
        assert all(value.split(" ", 1)[1] in registered for value in values)
    assert headers.raw == original


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
@pytest.mark.parametrize("header", ["X-API-Key", "Authorization"])
def test_httpx_duplicate_final_headers_loopback_differential(
        fresh_state, monkeypatch, module, asynchronous, kind, header):
    httpx = importlib.import_module(module)
    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())
    values = ["Bearer DUPLICATE_FINAL_A_12345678,comma-part", "Bearer DUPLICATE_FINAL_B_12345678"]

    def late_headers(request):
        # Runs after capture begins, exercising final-dispatch observation rather
        # than best-effort early collection. Preserve distinct entries and commas.
        request.headers.update([(header, values[0]), (header.lower(), values[1])])

    async def async_late_headers(request):
        late_headers(request)

    def run(url):
        if asynchronous:
            async def send():
                async with httpx.AsyncClient(event_hooks={"request": [async_late_headers]}) as client:
                    response = await client.post(url, json=CHAT)
                    return response.status_code, response.content, response.request.headers.raw
            return asyncio.run(send())
        with httpx.Client(event_hooks={"request": [late_headers]}) as client:
            response = client.post(url, json=CHAT)
            return response.status_code, response.content, response.request.headers.raw

    with echo_server(credential_header=header) as server:
        url = server + "/v1/chat/completions"
        baseline = run(url)
        provider, app, fi = install_agent()
        with FITracer(provider.get_tracer("library")).start_as_current_span(
                "outer-" + kind, attributes={"gen_ai.span.kind": kind}) as outer:
            with provider.get_tracer("native").start_as_current_span("anthropic.messages.create") as native:
                actual = run(url)
                native.set_attribute("output.value", actual[1].decode())
            outer.set_attribute("output.value", actual[1].decode())
        assert actual == baseline
        assert all(value in json.loads(actual[1])["choices"][0]["message"]["content"] for value in values)
        assert all(value in _secrets.snapshot() for value in values)
    assert_process_exports(provider, (app, fi), values)
    for exporter in (app, fi):
        spans = exporter.get_finished_spans()
        assert len(spans) == (3 if kind == "LLM" else 4)
        assert sum(s.attributes.get("traceai.capture.layer") == "wire" for s in spans) == (kind == "CHAIN")
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
def test_requests_redirect_adapter_observes_new_query_differential(fresh_state, kind):
    import requests
    def run(url):
        with requests.Session() as client:
            response = client.post(url, json=CHAT)
            return response.status_code, response.content, [r.status_code for r in response.history]
    with echo_server() as server:
        url = server + "/redirect/v1/chat/completions"
        baseline = run(url)
        provider, app, fi = install_agent()
        with FITracer(provider.get_tracer("library")).start_as_current_span(
                "outer-" + kind, attributes={"gen_ai.span.kind": kind}):
            assert run(url) == baseline
    assert_process_exports(provider, (app, fi), ["REDIRECT_QUERY_SECRET_1234"])
    _manager.shutdown()
    provider.shutdown()


def test_suppressed_aiohttp_does_not_serialize_or_parse_body_for_observation(fresh_state):
    aiohttp = pytest.importorskip("aiohttp")
    calls = []
    body_secret = "SUPPRESSED_BODY_NOT_PARSED_12345678"
    def serialize(value):
        calls.append(value)
        return json.dumps(value)
    async def run(url):
        async with aiohttp.ClientSession(json_serialize=serialize) as client:
            async with client.post(url, json={**CHAT, "client_secret": body_secret}) as response:
                return response.status, await response.read()
    with echo_server() as server:
        url = server + "/v1/chat/completions"
        baseline = asyncio.run(run(url))
        assert len(calls) == 1
        calls.clear()
        provider, app, fi = install_agent()
        with FITracer(provider.get_tracer("library")).start_as_current_span(
                "model", attributes={"gen_ai.span.kind": "LLM"}):
            assert asyncio.run(run(url)) == baseline
        assert len(calls) == 1
        assert body_secret not in _secrets.snapshot()
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("secret", ["ZERO_CODE_BYTES_AUTH_12345678", "LATIN1_BYTES_AUTH_é_12345678"])
def test_requests_bytes_credentials_differential(fresh_state, monkeypatch, kind, streamed, secret):
    import requests

    # Each variant must discover its own credential, not reuse a prior observation.
    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())
    authorization = ("Bearer " + secret).encode("latin-1")

    def run(url):
        with requests.Session() as client:
            response = client.post(url, json=CHAT, headers={"Authorization": authorization}, stream=streamed)
            assert response.request.headers["Authorization"] is authorization
            return response.status_code, response.content, [r.status_code for r in response.history]

    with echo_server() as server:
        url = server + "/v1/chat/completions"
        baseline = run(url)
        provider, app, fi = install_agent()
        with FITracer(provider.get_tracer("library")).start_as_current_span(
                "outer-" + kind, attributes={"gen_ai.span.kind": kind}) as outer:
            with provider.get_tracer("native").start_as_current_span("anthropic.messages.create") as native:
                actual = run(url)
                native.set_attribute("output.value", actual[1].decode())
            outer.set_attribute("output.value", actual[1].decode())
        assert actual == baseline
        assert secret in json.loads(actual[1])["choices"][0]["message"]["content"]
        registered = secret in _secrets.snapshot() and authorization.decode("latin-1") in _secrets.snapshot()
        assert registered  # Never include registry contents in assertion diagnostics.
    assert_process_exports(provider, (app, fi), [secret, authorization.decode("latin-1")])
    for exporter in (app, fi):
        spans = exporter.get_finished_spans()
        assert len(spans) == (3 if kind == "LLM" else 4)
        assert sum(s.attributes.get("traceai.capture.layer") == "wire" for s in spans) == (kind == "CHAIN")
    _manager.shutdown()
    provider.shutdown()


def test_observe_bytes_headers_uses_builtin_decode_without_user_hooks(monkeypatch):
    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())

    class Header(bytes):
        def decode(self, *args, **kwargs):
            raise AssertionError("never call subclass decode")

        def __str__(self):
            raise AssertionError("never stringify")

    _privacy.observe_request({Header(b"Authorization"): Header(b"Bearer BYTES_HEADER_SECRET_12345678"),
                              b"X-API-Key": b"BYTES_API_KEY_12345678"})
    assert set(_secrets.snapshot()) == {"Bearer BYTES_HEADER_SECRET_12345678",
                                       "BYTES_HEADER_SECRET_12345678", "BYTES_API_KEY_12345678"}


@pytest.mark.parametrize("key", ["input.audio.0", "input.images.0", "output.audio.0.1",
                                 "gen_ai.input.messages.0.inlineData.data", "input.audio", "input.images"])
@pytest.mark.parametrize("value", ["YWJj" * 100, ["YWJj" * 100, "ZGVm" * 100]])
def test_indexed_media_paths_are_structurally_scrubbed(key, value):
    # Unknown base64 obeys the S6 >256-character threshold; small audio and
    # magic media are independently pinned by the shared catalog fixtures.
    original = {key: value}
    cleaned = _privacy.scrub(original)
    assert "YWJj" * 100 not in repr(cleaned) and "ZGVm" * 100 not in repr(cleaned)
    assert original == {key: value}


def test_indexed_media_and_nested_audio_scrub_every_export_surface(fresh_state):
    from auto_mock_server import MockServer, decode_spans
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    attributes = {"input.audio.0": "YQ==", "input.images.0": "iVBORw0KGgo=",
                  "input.audio": ["YQ==", "Yg=="], "output.audio.0.1": "Yw==",
                  "input.value": json.dumps({"nested": {"audio": ["ZA==", "ZQ=="]},
                                             "inlineData": {"mimeType": "audio/wav", "data": "Zg=="}}),
                  "gen_ai.audio.transcript": "hello", "gen_ai.audio.mime_type": "audio/wav"}
    with MockServer() as collector:
        fi = OTLPSpanExporter(endpoint=collector.url + "/tracer/v1/traces")
        app = InMemorySpanExporter()
        provider = TracerProvider(resource=Resource(attributes), shutdown_on_exit=False)
        provider.add_span_processor(SimpleSpanProcessor(app))
        _manager.init(tracer_provider=provider, span_exporter=fi, batch=False, instrumentations=[])
        tracer = provider.get_tracer("application", attributes=attributes)
        with tracer.start_as_current_span("media-flat", attributes=attributes, links=[
                trace.Link(trace.SpanContext(123, 456, False), attributes)]) as span:
            span.add_event("media", attributes)
        for exported in (snapshot(app.get_finished_spans()[0]), repr(decode_spans(collector.collected))):
            assert all(raw not in exported for raw in ("YQ==", "Yg==", "Yw==", "ZA==", "ZQ==", "Zg==", "iVBORw0KGgo="))
            assert "hello" in exported and "audio/wav" in exported
    assert attributes["input.audio.0"] == "YQ=="  # No mutation of application data.
    _manager.shutdown()
    provider.shutdown()


def test_registry_fork_reset_releases_inherited_lock_without_losing_values():
    registry = _secrets._SecretRegistry()
    registry.register("INHERITED_CREDENTIAL_12345678")
    old_lock = registry._lock
    old_lock.acquire()
    try:
        registry._after_fork()
        assert registry._lock.acquire(blocking=False)
        registry._lock.release()
        assert registry.snapshot() == ("INHERITED_CREDENTIAL_12345678",)
    finally:
        old_lock.release()


def test_registry_is_bounded_lru_longest_first_thread_safe_and_quiet(monkeypatch, caplog):
    registry = _secrets._SecretRegistry()
    monkeypatch.setattr(_secrets, "_registry", registry)
    register_secret("short")
    class Untrusted:
        def __str__(self):
            raise AssertionError("never stringify")
    register_secret(Untrusted())
    assert registry.snapshot() == ()
    values = [f"CREDENTIAL_{i:04}" for i in range(1024)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(register_secret, values))
    assert len(registry.snapshot()) == 1024
    for value in values:
        register_secret(value)  # Establish deterministic recency after concurrent writes.
    register_secret(values[0])  # refresh, then evict oldest unrefreshed
    register_secret("CREDENTIAL_1024_LONGEST")
    snapshot_values = registry.snapshot()
    assert len(snapshot_values) == 1024 and values[0] in snapshot_values
    assert values[1] not in snapshot_values
    assert snapshot_values[0] == "CREDENTIAL_1024_LONGEST"
    assert not caplog.text
    assert "CREDENTIAL" not in repr(registry)
    assert _privacy.scrub("CREDENTIAL_1024_LONGEST", snapshot_values) == "<redacted>"


def test_environment_fi_and_explicit_headers_seed_at_init_and_not_export(fresh_state, monkeypatch):
    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())
    names = ["KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH"]
    values = ["INITIAL_ENV_" + name + "_12345678" for name in names]
    for name, value in zip(names, values):
        monkeypatch.setenv("APP_" + name + "_VALUE", value)
    monkeypatch.setenv("APP_SHORT_TOKEN", "short")
    fi_key, fi_secret, header = "FI_CONFIG_KEY_12345678", "FI_CONFIG_SECRET_12345678", "FI_HEADER_CREDENTIAL_12345678"
    custom_header = "FI_CUSTOM_COLLECTOR_CREDENTIAL_12345678"
    provider = _manager.init(span_exporter=InMemorySpanExporter(), instrumentations=[],
                             api_key=fi_key, secret_key=fi_secret, headers={
                                 "Authorization": "Bearer " + header, "X-Collector-Credential": custom_header})
    assert all(value in _secrets.snapshot() for value in values + [fi_key, fi_secret, header, custom_header])
    assert "short" not in _secrets.snapshot()
    monkeypatch.setenv("APP_LATE_TOKEN", "NOT_AT_INITIALIZATION_12345678")
    with provider.get_tracer("application").start_as_current_span("span"):
        pass
    assert "NOT_AT_INITIALIZATION_12345678" not in _secrets.snapshot()
    _manager.shutdown()
    provider.shutdown()


def test_prebuilt_otlp_exporter_credentials_protect_the_first_batch(fresh_state):
    from auto_mock_server import MockServer, decode_spans
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    secret = "PREBUILT_EXPORTER_CREDENTIAL_12345678"
    with MockServer() as collector:
        exporter = OTLPSpanExporter(endpoint=collector.url + "/tracer/v1/traces",
                                    headers={"Authorization": "Bearer " + secret})
        provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=[])
        with provider.get_tracer("application").start_as_current_span("first batch") as span:
            span.set_attribute("output.value", secret)
        assert _manager.flush(3000)
        exported = decode_spans(collector.collected)
        assert len(exported) == 1 and secret not in repr(exported)
        assert [r["headers"]["authorization"] for r in collector.requests] == ["Bearer " + secret]
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("headers", [{"X-Collector-Credential": "PREBUILT_METADATA_SECRET_12345678"},
                                    (("x-collector-credential", "PREBUILT_METADATA_SECRET_12345678"),)])
def test_prebuilt_exporter_mapping_or_grpc_metadata_seeds_registry(headers):
    class Exporter(InMemorySpanExporter):
        _headers = headers
    _manager._PrivacyExporter(Exporter(), TraceConfig())
    assert "PREBUILT_METADATA_SECRET_12345678" in _secrets.snapshot()


def test_registry_export_snapshot_covers_all_surfaces_and_fails_closed(monkeypatch):
    secret = "PUBLIC_REGISTERED_VALUE_12345678"
    register_secret(secret)
    ctx = trace.SpanContext(123, 456, False, trace.TraceFlags(1), trace.TraceState([("vendor", secret)]))
    original = ReadableSpan(name=secret, context=ctx, parent=ctx, resource=Resource({"echo": secret}),
        instrumentation_scope=InstrumentationScope(secret, secret, attributes={"echo": secret}),
        attributes={"output.value": secret, "array": [secret, secret]},
        status=trace.Status(trace.StatusCode.ERROR, secret), events=[Event(secret, {"echo": secret}, 15)],
        links=[trace.Link(ctx, {"echo": secret})], start_time=10, end_time=20)
    clean = _privacy.export_span(original, TraceConfig())
    assert secret not in snapshot(clean)
    assert clean.context.trace_state.get("vendor") == clean.links[0].context.trace_state.get("vendor") == "<redacted>"
    assert original.name == secret and original.attributes["output.value"] == secret
    def fail():
        raise RuntimeError("registry unavailable")
    monkeypatch.setattr(_secrets, "snapshot", fail)
    closed = _privacy.export_span(original, TraceConfig())
    assert closed.name == "span" and not closed.attributes and not closed.events and not closed.links
    assert not closed.resource.attributes and not closed.context.trace_state
    assert closed.context.trace_id == original.context.trace_id
    assert closed.start_time == 10 and closed.end_time == 20


def test_wire_registry_access_failure_keeps_span_identity_timing(fresh_state, monkeypatch):
    httpx = pytest.importorskip("httpx")
    provider, app, fi = install_agent()
    def fail():
        raise RuntimeError("registry unavailable")
    monkeypatch.setattr(_secrets, "snapshot", fail)
    payload = {"choices": [{"message": {"content": "sensitive output"}}]}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        assert client.post("http://127.0.0.1/v1/chat/completions", json=CHAT).json() == payload
    for exporter in (app, fi):
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "span" and not spans[0].attributes
        assert spans[0].context.trace_id and spans[0].end_time >= spans[0].start_time > 0
    _manager.shutdown()
    provider.shutdown()


def test_queued_span_uses_registry_at_export_not_span_start(fresh_state):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=True, schedule_delay_millis=60000,
                             instrumentations=[])
    secret = "LEARNED_AFTER_SPAN_END_12345678"
    with provider.get_tracer("application").start_as_current_span("already ended") as span:
        span.set_attribute("output.value", secret)
    register_secret(secret)
    assert _manager.flush(3000)
    assert exporter.get_finished_spans()[0].attributes["output.value"] == "<redacted>"
    _manager.shutdown()
    provider.shutdown()


def test_parsed_request_body_credentials_protect_unrelated_spans(fresh_state):
    httpx = pytest.importorskip("httpx")
    provider, app, fi = install_agent()
    secret = "PARSED_BODY_CREDENTIAL_12345678"
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices": [{"message": {"content": secret}}]}))) as client:
        response = client.post("http://127.0.0.1/v1/chat/completions", json={**CHAT, "nested": {"client_secret": secret}})
    assert secret in response.text
    assert_process_exports(provider, (app, fi), [secret])
    _manager.shutdown()
    provider.shutdown()
