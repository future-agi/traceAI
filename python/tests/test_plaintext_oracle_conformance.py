"""Every applicable row of the shared plain-channel oracle, run through this package's own exporters and requests.

The oracle is vendored as it was sealed (``fixtures/plaintext-rule-oracle.json.gz``). Each row is executed with this package's
marker keys against loopback listeners with byte counters (see ``oracle_conformance``): a refused row must raise the row's
error code and put no marker byte on any listener, a built row must reach the route the row names.
"""
import collections
import http.server
import threading

import pytest
import requests

import oracle_conformance as oc
from fi_instrumentation import otel, settings

OPT_IN = "FI_ALLOW_INSECURE_HTTP"
TRANSPORTS = ("http", "grpc", "eval-config")
RAW, ORACLE = oc.load_oracle()
# The suite replaces this function with a mock for every test; the original is taken at import time.
GET_TEMPLATE = settings.get_custom_eval_template


@pytest.fixture(scope="module")
def listeners():
    found = oc.Listeners()
    yield found
    found.close()


@pytest.fixture(autouse=True)
def _clean_warnings():
    otel._PLAIN_TEXT_WARNED.clear()
    yield
    otel._PLAIN_TEXT_WARNED.clear()


# ---- the vendored file -------------------------------------------------------------------------------------------------

def test_the_vendored_oracle_is_the_sealed_file_in_both_forms():
    gz_digest, json_digest = oc.digests()
    assert gz_digest == oc.ORACLE_GZ_SHA256 == "bb75798b5c34c68e428ca9b31a17c8df33bfdede43844dd36ff728f70552d1eb"
    assert json_digest == oc.ORACLE_JSON_SHA256 == "c2072ae9e74dc80543e10fabaf54da26554bf12d3033c7bbe46067cf8aac4f1e"


def test_the_oracle_has_the_rows_and_dimensions_the_driver_reads():
    assert len(ORACLE["rows"]) == 58787
    assert set(ORACLE["dimensions"]["callers"]) == set(oc.CALLERS)
    assert set(ORACLE["dimensions"]["transports"]) == set(TRANSPORTS)
    assert {row["host"] for row in ORACLE["rows"]} == set(ORACLE["dimensions"]["hosts"])


def test_every_row_is_driven_or_listed_as_not_applicable():
    driven = skipped = 0
    for transport in TRANSPORTS:
        cases, not_applicable = oc.cases(ORACLE, transport)
        driven += len({id(row) for row, _, _ in cases})
        skipped += not_applicable
    assert driven + skipped == len(ORACLE["rows"])
    # The exporters of grpc and the eval-config requests take no client from the caller: 2 callers x 2 transports of rows.
    assert {t: oc.cases(ORACLE, t)[1] for t in TRANSPORTS} == {"http": 0, "grpc": 7456, "eval-config": 7406}


# ---- the rows ----------------------------------------------------------------------------------------------------------

def _cells(transport):
    cases, _ = oc.cases(ORACLE, transport)
    cells = collections.defaultdict(list)
    for case in cases:
        cells[(case[0]["source"], case[2])].append(case)
    return cells


def _run_cell(transport, cell, listeners):
    mismatches = []
    for row, opt_in, caller in _cells(transport)[cell]:
        outcome = oc.run_row(ORACLE, row, listeners, opt_in, caller)
        for problem in oc.problems(ORACLE, row, transport, caller, outcome):
            mismatches.append(f"{row['host']}|{row['env']}|{row['no_proxy']}|opt-in={opt_in!r}|caller={caller}: {problem}")
    assert mismatches == [], f"{len(mismatches)} rows differ, first: {mismatches[:5]}"


HTTP_CELLS = sorted(_cells("http"))
GRPC_CELLS = sorted(_cells("grpc"))
EVAL_CELLS = sorted(_cells("eval-config"))


@pytest.mark.parametrize("cell", HTTP_CELLS, ids=["|".join(c) for c in HTTP_CELLS])
def test_the_http_exporter_answers_every_row(cell, listeners):
    _run_cell("http", cell, listeners)


@pytest.mark.parametrize("cell", GRPC_CELLS, ids=["|".join(c) for c in GRPC_CELLS])
def test_the_grpc_exporter_answers_every_row(cell, listeners):
    pytest.importorskip("grpc")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    _run_cell("grpc", cell, listeners)


@pytest.mark.parametrize("cell", EVAL_CELLS, ids=["|".join(c) for c in EVAL_CELLS])
def test_the_eval_config_request_answers_every_row(cell, listeners):
    _run_cell("eval-config", cell, listeners)


def test_the_only_rows_this_package_answers_differently_are_the_ones_listed():
    assert oc.STRICTER_THAN_ORACLE == {"s1_at_in_path": "disputed_authority"}
    hosts = {row["host"] for row in ORACLE["rows"] if row["decision"] == "build" and row["source"] == "tls"}
    assert "s1_at_in_path" in hosts  # the oracle builds these, this package refuses them: they are the listed difference


# ---- the counter works: an unguarded send makes it non-zero ------------------------------------------------------------

UNGUARDED_ROW = {"http": "self_collector", "grpc": "self_collector", "eval-config": "self_collector"}


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_send_with_the_guard_taken_out_puts_marker_bytes_on_the_listener_where_the_guarded_send_puts_none(transport, listeners):
    if transport == "grpc":
        pytest.importorskip("grpc")
    row = next(r for r in ORACLE["rows"] if r["transport"] == transport and r["host"] == UNGUARDED_ROW[transport]
               and r["source"] == "http_scheme" and r["opt_in"] == "unset" and r["env"] == "none" and r["no_proxy"] == "none")
    assert row["decision"] == "refuse" and row["error"] == "insecure_self_hosted_target"
    guarded = oc.run_row(ORACLE, row, listeners, None, "none")
    assert (guarded["decision"], guarded["error"], guarded["marker_bytes"]) == ("refuse", "insecure_self_hosted_target", 0)
    unguarded = oc.run_row(ORACLE, row, listeners, None, "none", unguarded=True)
    assert unguarded["decision"] == "build" and unguarded["dialed"] == "target"
    assert unguarded["marker_bytes"] > 0


# ---- the environments that route a plain request, one named case each ----------------------------------------------------

def _http_send(listeners, env, base="http://example.com:{T}/", session=None, opt_in="1"):
    """Build the HTTP exporter for ``base`` (named by FI_BASE_URL) and send one export. Returns (error code, measured)."""
    ports = {"A": listeners.a.port, "B": listeners.b.port, "T": listeners.target.port}
    values = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    values.update({name: value.replace("{A}", str(ports["A"])).replace("{B}", str(ports["B"])).replace("{T}", str(ports["T"]))
                   for name, value in env.items()})
    values.update({"FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET, OPT_IN: opt_in,
                   "FI_BASE_URL": base.replace("{T}", str(ports["T"]))})
    listeners.reset()
    code = None
    with oc.environment(values), listeners.shim():
        try:
            exporter = otel.HTTPSpanExporter(timeout=0.4, **({"session": session} if session is not None else {}))
            try:
                exporter.export([oc.one_span()])
            finally:
                exporter.shutdown()
        except otel.UnsupportedFutureAGIEndpointError as error:
            code = error.code
    return code, listeners.measure()


PROXIED_ENVIRONMENTS = {
    "HTTP_PROXY with a NO_PROXY that does not cover the host": {"HTTP_PROXY": "http://127.0.0.1:{A}", "NO_PROXY": ".example.com"},
    "ALL_PROXY alone": {"ALL_PROXY": "http://127.0.0.1:{A}"},
    "upper-case and lower-case names with different proxies": {"HTTP_PROXY": "http://127.0.0.1:{A}", "http_proxy": "http://127.0.0.1:{B}"},
    "NO_PROXY with a port that is not the port": {"HTTP_PROXY": "http://127.0.0.1:{A}", "NO_PROXY": "example.com:1"},
    "NO_PROXY that is a network range": {"HTTP_PROXY": "http://127.0.0.1:{A}", "NO_PROXY": "10.0.0.0/8"},
}


@pytest.mark.parametrize("name", list(PROXIED_ENVIRONMENTS))
def test_a_session_of_this_package_sends_plain_requests_to_the_target_and_nothing_to_a_proxy(name, listeners):
    code, measured = _http_send(listeners, PROXIED_ENVIRONMENTS[name])
    assert code is None
    assert listeners.a.connections == 0 and listeners.b.connections == 0
    assert measured["dialed"] == "target" and measured["marker_bytes"] > 0


def _a_library_routes_it(listeners, env, host="example.com"):
    """Whether requests, or the standard library, routes ``http://example.com:T/`` through the proxy the environment names."""
    import urllib.request

    values = {name: None for name in oc.PROXY_NAMES}
    values.update({name: value.replace("{A}", str(listeners.a.port)).replace("{B}", str(listeners.b.port)) for name, value in env.items()})
    url = f"http://{host}:{listeners.target.port}/"
    with oc.environment(values):
        by_requests = requests.utils.select_proxy(url, requests.utils.get_environ_proxies(url)) is not None
        proxies = urllib.request.getproxies_environment()
        by_standard_library = bool((proxies.get("http") or proxies.get("all")) and not urllib.request.proxy_bypass_environment(f"{host}:{listeners.target.port}"))
    return by_requests or by_standard_library


@pytest.mark.parametrize("name", list(PROXIED_ENVIRONMENTS))
def test_a_session_the_caller_passes_is_refused_when_the_environment_routes_it_and_no_marker_byte_reaches_a_sink(name, listeners):
    code, measured = _http_send(listeners, PROXIED_ENVIRONMENTS[name], session=requests.Session())
    if _a_library_routes_it(listeners, PROXIED_ENVIRONMENTS[name]):
        assert code == "proxied"
        assert measured == {"dialed": None, "marker_bytes": 0}
    else:
        # A release of requests or of Python that reads the entry as a bypass: nothing routes the request, so it is not refused.
        assert code is None and measured["dialed"] == "target"
        assert listeners.a.connections == 0 and listeners.b.connections == 0


@pytest.mark.parametrize("name", [n for n in PROXIED_ENVIRONMENTS if not n.startswith("HTTP_PROXY with a NO_PROXY")])
def test_the_environments_that_every_release_routes_are_refused_for_a_session_the_caller_passes(name, listeners):
    code, measured = _http_send(listeners, PROXIED_ENVIRONMENTS[name], session=requests.Session())
    assert code == "proxied" and measured == {"dialed": None, "marker_bytes": 0}


def test_a_session_the_caller_passes_with_a_transport_that_cannot_be_inspected_is_refused_and_no_marker_byte_reaches_a_sink(listeners):
    session = oc.caller_session("uninspectable_transport", {"A": listeners.a.port})
    code, measured = _http_send(listeners, {}, session=session)
    assert code == "proxied"
    assert measured == {"dialed": None, "marker_bytes": 0}


def test_a_session_subclass_that_replaces_a_routing_method_cannot_be_inspected_and_is_refused(listeners):
    class Timeouts(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", 3)
            return super().request(*args, **kwargs)

    code, measured = _http_send(listeners, {}, session=Timeouts())
    assert code == "proxied" and measured["marker_bytes"] == 0


def test_a_session_the_caller_passes_that_nothing_routes_is_used_as_built(listeners):
    code, measured = _http_send(listeners, {}, session=requests.Session())
    assert code is None and measured["dialed"] == "target" and measured["marker_bytes"] > 0


# ---- a redirect on a request that carries keys is a refusal --------------------------------------------------------------

class Redirecting:
    """A loopback server that answers every request with a redirect to the proxy sink B."""

    def __init__(self, to_port):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - the method name http.server looks for
                outer.requests += 1
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{to_port}/followed")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                return None

        self.requests = 0
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def redirecting(listeners):
    found = Redirecting(listeners.b.port)
    yield found
    found.close()


def test_the_http_exporter_does_not_follow_a_redirect_and_no_marker_byte_reaches_the_target_of_it(redirecting, listeners):
    listeners.reset()
    env = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    env.update({"FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET, "FI_BASE_URL": f"http://127.0.0.1:{redirecting.port}"})
    with oc.environment(env):
        exporter = otel.HTTPSpanExporter(timeout=0.5)
        try:
            exporter.export([oc.one_span()])
        finally:
            exporter.shutdown()
    assert redirecting.requests >= 1
    measured = listeners.measure()
    assert listeners.b.connections == 0 and measured["marker_bytes"] == 0


def test_the_http_exporter_session_refuses_a_redirect_on_every_supported_release(redirecting, listeners):
    env = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    env.update({"FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET, "FI_BASE_URL": f"http://127.0.0.1:{redirecting.port}"})
    with oc.environment(env):
        exporter = otel.HTTPSpanExporter(timeout=0.5)
        try:
            session = otel._requests_session_of(exporter)
            assert session is not None
            listeners.reset()
            with pytest.raises(otel._RedirectRefused):
                session.post(f"http://127.0.0.1:{redirecting.port}/", data=b"x", headers={"x-api-key": oc.MARKER_KEY})
            assert listeners.b.connections == 0
        finally:
            exporter.shutdown()


def test_the_eval_config_request_does_not_follow_a_redirect(redirecting, listeners):
    listeners.reset()
    env = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    env.update({"FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET, "FI_BASE_URL": f"http://127.0.0.1:{redirecting.port}"})
    with oc.environment(env):
        assert oc.CHECK_EXISTS("project", ["tag"]) is False
        with pytest.raises(ValueError):
            GET_TEMPLATE("some-eval")
    measured = listeners.measure()
    assert redirecting.requests == 2
    assert listeners.b.connections == 0 and measured["marker_bytes"] == 0


def test_the_collector_check_does_not_follow_a_redirect(redirecting, listeners):
    from fi_instrumentation.auto import _cli

    listeners.reset()
    env = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    env.update({"FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET, "FI_BASE_URL": f"http://127.0.0.1:{redirecting.port}"})
    with oc.environment(env):
        ok, detail = _cli.check_collector(_cli.resolve())
    assert ok is False and "unreachable" in detail
    assert redirecting.requests == 1
    assert listeners.b.connections == 0 and listeners.measure()["marker_bytes"] == 0


def test_a_request_without_keys_is_sent_as_before_with_no_redirect_setting(monkeypatch):
    seen = {}

    class Reply:
        status_code = 307

    monkeypatch.setattr(requests, "post", lambda url, **kwargs: (seen.update(kwargs), Reply())[1])
    response = otel._post_with_keys("https://collector.example/x", False, json={})
    assert response.status_code == 307 and "allow_redirects" not in seen


# ---- key header names in the environment ---------------------------------------------------------------------------------

@pytest.mark.parametrize("variable", ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"])
@pytest.mark.parametrize("names", ["x-api-key", "X-Secret-Key", "x-trace=1,X-API-KEY"])
@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_key_header_names_in_the_environment_are_refused_for_an_exporter_that_is_not_bound_to_future_agi(variable, names, transport, listeners):
    if transport == "grpc":
        pytest.importorskip("grpc")
    listeners.reset()
    header = ",".join(part if "=" in part else f"{part}={oc.MARKER_KEY}" for part in names.split(","))
    env = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    env.update({variable: header, "FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET})
    target = f"127.0.0.1:{listeners.target.port}"
    with oc.environment(env), pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        if transport == "http":
            otel.HTTPSpanExporter(endpoint=f"http://{target}/v1/traces", timeout=0.4)
        else:
            otel.GRPCSpanExporter(endpoint=target, insecure=True, timeout=0.4)
    assert raised.value.code == "key_header_in_environment"
    assert oc.MARKER_KEY not in str(raised.value)
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_a_session_the_opentelemetry_release_loads_itself_and_that_cannot_be_inspected_is_refused_on_a_plain_channel(listeners, monkeypatch):
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as trace_exporter

    if not hasattr(trace_exporter, "_load_session_from_envvar"):
        pytest.skip("this release of the exporter has no session loader")

    class Loaded(requests.Session):
        def request(self, *args, **kwargs):
            return super().request(*args, **kwargs)

    monkeypatch.setattr(trace_exporter, "_load_session_from_envvar", lambda *a, **k: Loaded())
    code, measured = _http_send(listeners, {})
    assert code == "proxied" and measured["marker_bytes"] == 0
