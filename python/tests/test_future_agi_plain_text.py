"""Future AGI keys over a plain channel: loopback is allowed, Future AGI hosts use TLS, any other host needs an opt-in."""
import logging
import socket
import threading
from concurrent import futures

import pytest
import requests

from fi_instrumentation import otel, settings

FI_KEY, FI_SECRET = "synthetic-env-key", "synthetic-env-secret"
OPT_IN = "FI_ALLOW_INSECURE_HTTP"
GET_TEMPLATE = settings.get_custom_eval_template
CHECK_EXISTS = otel.check_custom_eval_config_exists
REAL_POST = requests.post
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "FI_TRACES_URL", OPT_IN,
                 "OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_TRACES_INSECURE", *PROXY_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "127.0.0.1:9")
    otel._PLAIN_TEXT_WARNED.clear()
    yield
    otel._PLAIN_TEXT_WARNED.clear()


def _non_loopback_address():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
    except OSError:
        address = "127.0.0.1"
    finally:
        probe.close()
    return address


NON_LOOPBACK = _non_loopback_address()
needs_non_loopback = pytest.mark.skipif(NON_LOOPBACK.startswith("127."), reason="the machine has no non-loopback address")


class _Reply:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"result": {"exists": False}}


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def post(url, headers=None, **kwargs):
        calls.append((url, {k.lower(): v for k, v in (headers or {}).items()}))
        return _Reply()

    monkeypatch.setattr(requests, "post", post)
    return calls


def _http(base, **kwargs):
    exporter = otel.HTTPSpanExporter(endpoint=base, timeout=2, **kwargs) if base else otel.HTTPSpanExporter(timeout=2, **kwargs)
    exporter.shutdown()


def _template(base=None, **kwargs):
    return GET_TEMPLATE("some-eval", base_url=base, **kwargs)


def _exists(base=None, **kwargs):
    return CHECK_EXISTS("project", ["tag"], base_url=base, **kwargs)


HTTP_SITES = {"exporter": lambda **kw: _http(None, **kw), "eval-template": lambda **kw: _template(**kw),
              "eval-exists": lambda **kw: _exists(**kw)}


def _set_base(monkeypatch, base):
    monkeypatch.setenv("FI_BASE_URL", base)


# ---- the rule for HTTP and the eval requests ------------------------------------------------------------------

@pytest.mark.parametrize("base", ["http://127.0.0.1:4318", "http://localhost:4318", "http://[::1]:4318"])
@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_loopback_http_with_keys_is_allowed_without_an_opt_in_or_a_warning(site, base, monkeypatch, sent, caplog):
    _set_base(monkeypatch, base)
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        HTTP_SITES[site]()
    assert not [r for r in caplog.records if OPT_IN in r.getMessage()]


@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_self_hosted_http_is_refused_by_default_with_one_value_free_error_that_names_the_opt_in(site, monkeypatch, sent):
    _set_base(monkeypatch, "http://collector.internal:8080/path?token=secret-value")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        HTTP_SITES[site]()
    message = str(raised.value)
    assert OPT_IN in message
    for hidden in (FI_KEY, FI_SECRET, "secret-value", "/path", "collector.internal"):
        assert hidden not in message
    assert sent == []


@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_the_opt_in_allows_self_hosted_http_with_one_warning_naming_the_setting_and_the_origin(site, monkeypatch, sent, caplog):
    _set_base(monkeypatch, "http://collector.internal:8080/path?token=secret-value")
    monkeypatch.setenv(OPT_IN, "1")
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        for _ in range(2):
            HTTP_SITES[site]()
    warnings = [r.getMessage() for r in caplog.records if OPT_IN in r.getMessage()]
    assert len(warnings) == 1, caplog.text
    assert "http://collector.internal:8080" in warnings[0]
    for hidden in (FI_KEY, FI_SECRET, "secret-value", "/path"):
        assert hidden not in warnings[0]


@pytest.mark.parametrize("value", ["true", "yes", "0", " 1", "1 ", ""])
def test_only_the_exact_value_one_opts_in(value, monkeypatch, sent):
    _set_base(monkeypatch, "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, value)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _http(None)


def test_the_constructor_option_opts_in_for_that_exporter(monkeypatch, sent, caplog):
    _set_base(monkeypatch, "http://collector.internal:8080")
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        _http(None, allow_insecure_http=True)
    assert len([r for r in caplog.records if OPT_IN in r.getMessage()]) == 1
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _http(None)


@pytest.mark.parametrize("site", list(HTTP_SITES))
@pytest.mark.parametrize("base", ["http://api.futureagi.com", "http://api.futureagi.com./", "http://futureagi.com"])
def test_the_opt_in_never_applies_to_a_future_agi_host(site, base, monkeypatch, sent):
    _set_base(monkeypatch, base)
    monkeypatch.setenv(OPT_IN, "1")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        HTTP_SITES[site]()
    assert sent == []


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "http_proxy", "ALL_PROXY"])
@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_the_opt_in_is_refused_when_the_request_would_go_through_a_proxy(site, variable, monkeypatch, sent):
    _set_base(monkeypatch, "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv(variable, "http://proxy.internal:3128")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        HTTP_SITES[site]()
    assert "proxy.internal" not in str(raised.value) and FI_KEY not in str(raised.value)
    assert sent == []


def test_a_proxy_that_the_no_proxy_list_skips_does_not_block_the_opt_in(monkeypatch, sent):
    _set_base(monkeypatch, "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("no_proxy", "collector.internal")
    _http(None)


def test_the_opt_in_is_refused_when_the_callers_session_names_a_proxy(monkeypatch, sent):
    _set_base(monkeypatch, "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    session = requests.Session()
    session.proxies = {"http": "http://proxy.internal:3128"}
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _http(None, session=session)


@needs_non_loopback
def test_no_key_bytes_reach_a_self_hosted_http_listener_by_default(monkeypatch):
    received = bytearray()
    server = socket.socket()
    server.bind((NON_LOOPBACK, 0))
    server.listen(2)
    port = server.getsockname()[1]

    def accept():
        try:
            conn, _ = server.accept()
            received.extend(conn.recv(65536))
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    _set_base(monkeypatch, f"http://{NON_LOOPBACK}:{port}")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.HTTPSpanExporter(timeout=2)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        CHECK_EXISTS("project", ["tag"])
    server.close()
    assert bytes(received) == b""


# ---- gRPC ---------------------------------------------------------------------------------------------------

grpc = pytest.importorskip("grpc")
pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")


def _grpc(endpoint=None, **kwargs):
    exporter = otel.GRPCSpanExporter(timeout=2, **({"endpoint": endpoint} if endpoint else {}), **kwargs)
    exporter.shutdown()


@pytest.mark.parametrize("target", ["127.0.0.1:50051", "http://127.0.0.1:50051", "localhost:50051", "[::1]:50051"])
def test_loopback_grpc_plaintext_is_allowed_without_an_opt_in_or_a_warning(target, monkeypatch, caplog):
    monkeypatch.setenv("FI_GRPC_URL", target)
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        _grpc(insecure=True)
    assert not [r for r in caplog.records if OPT_IN in r.getMessage()]


PLAINTEXT_REQUESTS = {
    "http-scheme": ("http://collector.internal:50051", {}, {}),
    "insecure-argument": ("collector.internal:50051", {"insecure": True}, {}),
    "insecure-env": ("collector.internal:50051", {}, {"OTEL_EXPORTER_OTLP_INSECURE": "true"}),
    "insecure-traces-env": ("collector.internal:50051", {}, {"OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true"}),
}


@pytest.mark.parametrize("name", list(PLAINTEXT_REQUESTS))
def test_self_hosted_grpc_plaintext_is_refused_by_default_and_names_the_opt_in(name, monkeypatch):
    target, kwargs, env = PLAINTEXT_REQUESTS[name]
    monkeypatch.setenv("FI_GRPC_URL", target)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        _grpc(**kwargs)
    assert OPT_IN in str(raised.value) and FI_KEY not in str(raised.value) and "collector.internal" not in str(raised.value)


def test_self_hosted_grpc_without_a_plaintext_request_uses_tls(monkeypatch):
    monkeypatch.setenv("FI_GRPC_URL", "collector.internal:50051")
    kinds = []
    real_secure, real_insecure = grpc.secure_channel, grpc.insecure_channel
    import opentelemetry.exporter.otlp.proto.grpc.exporter as base

    for module in (grpc, base):
        if hasattr(module, "secure_channel"):
            monkeypatch.setattr(module, "secure_channel", lambda *a, _r=real_secure, **k: (kinds.append("secure"), _r(*a, **k))[1])
            monkeypatch.setattr(module, "insecure_channel", lambda *a, _r=real_insecure, **k: (kinds.append("insecure"), _r(*a, **k))[1])
    exporter = otel.GRPCSpanExporter(timeout=2)
    exporter.shutdown()
    assert "insecure" not in kinds


@pytest.mark.parametrize("name", list(PLAINTEXT_REQUESTS))
def test_the_opt_in_allows_self_hosted_grpc_plaintext_with_one_warning(name, monkeypatch, caplog):
    target, kwargs, env = PLAINTEXT_REQUESTS[name]
    monkeypatch.setenv("FI_GRPC_URL", target)
    monkeypatch.setenv(OPT_IN, "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        _grpc(**kwargs)
        _grpc(**kwargs)
    warnings = [r.getMessage() for r in caplog.records if OPT_IN in r.getMessage()]
    assert len(warnings) == 1, caplog.text
    assert "collector.internal:50051" in warnings[0] and FI_KEY not in warnings[0]


@pytest.mark.parametrize("name", ["http-scheme", "insecure-argument", "insecure-env"])
def test_the_opt_in_never_makes_a_future_agi_grpc_host_plaintext(name, monkeypatch):
    target, kwargs, env = PLAINTEXT_REQUESTS[name]
    host = "grpc.futureagi.com"
    monkeypatch.setenv(OPT_IN, "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    endpoint = ("http://" if name == "http-scheme" else "") + f"{host}:443"
    kinds = []
    import opentelemetry.exporter.otlp.proto.grpc.exporter as base

    for module in (grpc, base):
        if hasattr(module, "insecure_channel"):
            monkeypatch.setattr(module, "insecure_channel", lambda *a, **k: kinds.append("insecure"))
    try:
        otel.GRPCSpanExporter(endpoint=endpoint, timeout=2, **kwargs).shutdown()
    except otel.UnsupportedFutureAGIEndpointError:
        pass
    assert kinds == []


def test_the_opt_in_is_refused_for_grpc_through_a_proxy(monkeypatch):
    monkeypatch.setenv("FI_GRPC_URL", "http://collector.internal:50051")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _grpc()


@needs_non_loopback
def test_a_plaintext_grpc_server_on_a_non_loopback_address_receives_spans_with_the_opt_in(monkeypatch):
    seen = []

    def export(request, context):
        seen.append({k.lower(): v for k, v in context.invocation_metadata()})
        return b""

    handler = grpc.method_handlers_generic_handler(
        "opentelemetry.proto.collector.trace.v1.TraceService", {"Export": grpc.unary_unary_rpc_method_handler(export)}
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((handler,))
    port = server.add_insecure_port(f"{NON_LOOPBACK}:0")
    server.start()
    try:
        monkeypatch.setenv("FI_GRPC_URL", f"http://{NON_LOOPBACK}:{port}")
        monkeypatch.setenv(OPT_IN, "1")
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        exporter = otel.GRPCSpanExporter(timeout=3)
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        with provider.get_tracer("plain").start_as_current_span("span"):
            pass
        provider.shutdown()
    finally:
        server.stop(None)
    assert seen and seen[0].get("x-api-key") == FI_KEY


# ---- hosts a parser rewrites into loopback are not loopback ---------------------------------------------------

WRITTEN_HOSTS = ["127.0.0.1.", "127.1", "0x7f.0.0.1", "2130706433", "localhost.", "app.localhost"]


@pytest.mark.parametrize("host", WRITTEN_HOSTS)
@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_a_written_host_that_is_not_literal_loopback_is_refused_over_http_by_default(site, host, monkeypatch, sent):
    _set_base(monkeypatch, f"http://{host}:4318")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        HTTP_SITES[site]()
    assert OPT_IN in str(raised.value)
    assert sent == []


@pytest.mark.parametrize("host", WRITTEN_HOSTS)
@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_a_written_host_that_is_not_literal_loopback_is_self_hosted_with_the_opt_in(site, host, monkeypatch, sent, caplog):
    _set_base(monkeypatch, f"http://{host}:4318")
    monkeypatch.setenv(OPT_IN, "1")
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        HTTP_SITES[site]()
        HTTP_SITES[site]()
    warnings = [r.getMessage() for r in caplog.records if OPT_IN in r.getMessage()]
    assert len(warnings) == 1 and f"http://{host}:4318" in warnings[0]


@pytest.mark.parametrize("host", WRITTEN_HOSTS)
@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_a_written_host_that_is_not_literal_loopback_is_refused_through_a_proxy_even_with_the_opt_in(site, host, monkeypatch, sent):
    _set_base(monkeypatch, f"http://{host}:4318")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        HTTP_SITES[site]()
    assert sent == []


@pytest.mark.parametrize("host", WRITTEN_HOSTS)
def test_a_written_host_that_is_not_literal_loopback_follows_the_same_grpc_cells(host, monkeypatch):
    monkeypatch.setenv("FI_GRPC_URL", f"http://{host}:50051")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        _grpc()
    assert OPT_IN in str(raised.value)
    monkeypatch.setenv(OPT_IN, "1")
    _grpc()
    monkeypatch.setenv("http_proxy", "http://proxy.internal:3128")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _grpc()
