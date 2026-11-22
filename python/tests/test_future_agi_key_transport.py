"""One proxy rule, parser disagreement, the final key set and empty authorities for requests that carry Future AGI keys."""
import logging
import socket
import threading

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


class _Reply:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"result": {"exists": False}}


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, headers=None, **kw: (calls.append((url, headers)), _Reply())[1])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, headers=None, **kw: (calls.append((url, headers)), _Reply())[1])
    return calls


def _http():
    otel.HTTPSpanExporter(timeout=2).shutdown()


def _grpc():
    pytest.importorskip("grpc")
    otel.GRPCSpanExporter(timeout=2).shutdown()


def _template():
    return GET_TEMPLATE("some-eval")


def _exists():
    return CHECK_EXISTS("project", ["tag"])


HTTP_SITES = {"exporter": _http, "eval-template": _template, "eval-exists": _exists}


# ---- one proxy rule -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_an_https_proxy_alone_does_not_route_an_http_request(site, monkeypatch, sent):
    monkeypatch.setenv("FI_BASE_URL", "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    HTTP_SITES[site]()


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"])
@pytest.mark.parametrize("site", list(HTTP_SITES))
def test_an_http_or_all_proxy_does_not_route_a_plain_request_this_package_builds(site, variable, monkeypatch, sent):
    monkeypatch.setenv("FI_BASE_URL", "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv(variable, "http://proxy.internal:3128")
    HTTP_SITES[site]()


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"])
def test_an_http_or_all_proxy_routes_a_plain_request_of_a_session_the_caller_passes(variable, monkeypatch, sent):
    monkeypatch.setenv("FI_BASE_URL", "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv(variable, "http://proxy.internal:3128")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        otel.HTTPSpanExporter(timeout=2, session=requests.Session())
    assert raised.value.code == "proxied"
    assert sent == []


@pytest.mark.parametrize("no_proxy", ["collector.internal", ".internal", "*", "collector.internal:8080", "other.example, collector.internal", "internal"])
def test_a_no_proxy_entry_bypasses_the_proxy_for_a_session_the_caller_passes(no_proxy, monkeypatch, sent):
    monkeypatch.setenv("FI_BASE_URL", "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", no_proxy)
    otel.HTTPSpanExporter(timeout=2, session=requests.Session()).shutdown()


@pytest.mark.parametrize("no_proxy", ["collector.internal:9999", "10.0.0.0/8", "other.example"])
def test_a_no_proxy_entry_that_does_not_match_leaves_the_proxy_in_force_for_a_session_the_caller_passes(no_proxy, monkeypatch, sent):
    monkeypatch.setenv("FI_BASE_URL", "http://10.0.0.5:8080" if "/" in no_proxy else "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", no_proxy)
    # A network range is read by requests and not by the standard library; either one that routes the request refuses it.
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.HTTPSpanExporter(timeout=2, session=requests.Session())


def test_a_callers_session_with_its_own_proxies_keeps_the_refusal(monkeypatch):
    monkeypatch.setenv("FI_BASE_URL", "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    session = requests.Session()
    session.proxies = {"https": "http://proxy.internal:3128"}
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.HTTPSpanExporter(timeout=2, session=session)


def test_an_https_proxy_alone_does_not_route_plain_grpc(monkeypatch):
    monkeypatch.setenv("FI_GRPC_URL", "http://collector.internal:50051")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    _grpc()
    monkeypatch.setenv("http_proxy", "http://proxy.internal:3128")
    _grpc()


# ---- raw listeners --------------------------------------------------------------------------------------------

class _Listener:
    def __init__(self):
        self.received = bytearray()
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            conn.settimeout(1)
            try:
                self.received.extend(conn.recv(65536))
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._server.close()


@pytest.fixture
def listener(monkeypatch):
    server = _Listener()
    monkeypatch.setattr(requests, "post", REAL_POST)
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: real("127.0.0.1" if host in ("evil.example", "api.futureagi.com", "grpc.futureagi.com") else host, *a, **k))
    yield server
    server.close()


def _http_site_with_base(site, base, monkeypatch):
    monkeypatch.setenv("FI_BASE_URL", base)
    monkeypatch.setenv(OPT_IN, "1")
    return HTTP_SITES[site]


# S1: the written host and the host a client dials disagree, or the authority has a backslash or userinfo.
S1_HTTP = [
    "http://localhost\\@evil.example:{port}",
    "https://localhost\\@evil.example:{port}",
    "http://api.futureagi.com\\@evil.example:{port}",
    "https://api.futureagi.com\\@evil.example:{port}",
    "http://localhost%5C@evil.example:{port}",
    "http://api.futureagi.com%5c@evil.example:{port}",
    "http://localhost:{port}/path@evil.example",
    "http://user@evil.example:{port}",
    "http://localhost\\.evil.example:{port}",
]


@pytest.mark.parametrize("site", list(HTTP_SITES))
@pytest.mark.parametrize("form", S1_HTTP)
def test_a_disputed_authority_is_refused_and_no_key_bytes_reach_a_listener(site, form, listener, monkeypatch):
    run = _http_site_with_base(site, form.replace("{port}", str(listener.port)), monkeypatch)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        run()
    assert FI_KEY not in str(raised.value) and FI_SECRET not in str(raised.value)
    assert bytes(listener.received) == b""


S1_GRPC = [
    "grpc://localhost\\@127.0.0.1:{port}",
    "grpc://grpc.futureagi.com\\@127.0.0.1:{port}",
    "grpc://localhost%5C@127.0.0.1:{port}",
    "http://localhost\\@127.0.0.1:{port}",
    "user@127.0.0.1:{port}",
    "127.0.0.1:{port}\\@grpc.futureagi.com",
]


@pytest.mark.parametrize("form", S1_GRPC)
def test_a_disputed_grpc_target_is_refused_and_no_key_bytes_reach_a_listener(form, listener, monkeypatch):
    monkeypatch.setenv("FI_GRPC_URL", form.replace("{port}", str(listener.port)))
    monkeypatch.setenv(OPT_IN, "1")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _grpc()
    assert bytes(listener.received) == b""


@pytest.mark.parametrize("target", ["grpc.futureagi.com:443\n", "https://grpc.futureagi.com:443\n", "grpc.futureagi.com:443 ", "grpc.futureagi.com:443\t"])
def test_a_host_that_differs_only_by_whitespace_is_refused(target, monkeypatch):
    monkeypatch.setenv("FI_GRPC_URL", target)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _grpc()
    assert otel._future_agi_decision(target, grpc=True) == "refuse"


# S3: an empty written authority is refused and never re-read from the path.
S3 = ["https:///api.futureagi.com/", "http:///api.futureagi.com", "https:///evil.example:{port}/", "http:///localhost/"]


@pytest.mark.parametrize("site", list(HTTP_SITES))
@pytest.mark.parametrize("form", S3)
def test_an_empty_authority_is_refused_and_no_key_bytes_reach_a_listener(site, form, listener, monkeypatch):
    run = _http_site_with_base(site, form.replace("{port}", str(listener.port)), monkeypatch)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        run()
    assert bytes(listener.received) == b""


def test_an_own_collector_with_userinfo_given_as_an_argument_is_still_built_without_keys(monkeypatch):
    exporter = otel.HTTPSpanExporter(endpoint="https://user:pw@collector.example/v1/traces", timeout=2)
    exporter.shutdown()


# S2: the decision uses the final outgoing key set, not the headers option alone.
@pytest.mark.parametrize("variable", ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"])
def test_keys_in_the_environment_headers_count_when_the_endpoint_names_future_agi(variable, monkeypatch):
    pytest.importorskip("grpc")
    for name in ("FI_API_KEY", "FI_SECRET_KEY"):
        monkeypatch.delenv(name)
    monkeypatch.setenv(variable, "x-api-key=synthetic-ambient-key,x-secret-key=synthetic-ambient-secret")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.GRPCSpanExporter(endpoint="grpc.futureagi.com:8443", timeout=2)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.HTTPSpanExporter(endpoint="http://api.futureagi.com/tracer/v1/traces", timeout=2)


def test_key_header_names_in_the_environment_are_refused_for_an_exporter_that_does_not_send_to_future_agi(monkeypatch):
    pytest.importorskip("grpc")
    for name in ("FI_API_KEY", "FI_SECRET_KEY"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=synthetic-ambient-key")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        otel.GRPCSpanExporter(endpoint="collector.example:4317", headers={"x-trace": "1"}, timeout=2)
    assert raised.value.code == "key_header_in_environment" and "synthetic-ambient-key" not in str(raised.value)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.HTTPSpanExporter(endpoint="https://collector.example/v1/traces", timeout=2)


def test_other_header_names_in_the_environment_are_left_to_the_collector_the_exporter_sends_to(monkeypatch):
    pytest.importorskip("grpc")
    for name in ("FI_API_KEY", "FI_SECRET_KEY"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-collector-token=synthetic-token")
    otel.GRPCSpanExporter(endpoint="collector.example:4317", timeout=2).shutdown()
    otel.HTTPSpanExporter(endpoint="https://collector.example/v1/traces", timeout=2).shutdown()
