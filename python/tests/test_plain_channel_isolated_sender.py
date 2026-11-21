"""A plain channel that carries Future AGI keys sends through an isolated copy of the session, built when the exporter is.

The copy has no proxies, does not read the environment, carries no hooks and keeps the TLS settings of the session it was made
from. What sends is judged on every export, not what the caller holds: a caller who changes their own session afterwards
cannot reach the copy. Marker key bytes are counted at the target listener and at two CONNECT-aware proxy sinks. The same cases
run on every installed OpenTelemetry release (the CI matrix runs 1.29.0 and 1.45.2).
"""
import logging
import ssl
import subprocess
import threading
import time
from urllib.parse import quote

import pytest
import requests
from opentelemetry.sdk.trace.export import SpanExportResult

import oracle_conformance as oc
from fi_instrumentation import otel
from test_plaintext_post_build_session import BASE, OPT_IN, LeakyAdapter, _env, _leak

SUCCESS = SpanExportResult.SUCCESS


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


def _proxy(listeners):
    return f"http://127.0.0.1:{listeners.a.port}"


def _install_sender(exporter, session):
    """Replace the session an exporter sends with, as its release holds it (``_session`` on 1.29, the transport's from 1.4x)."""
    if hasattr(exporter, "_session"):
        exporter._session = session
    else:
        exporter._client._transport._session = session


# ---- the caller's session changes while exports run ------------------------------------------------------------------------

@pytest.mark.parametrize("pause", [0.001, 0.0])
def test_a_caller_session_flipped_during_a_thousand_exports_puts_no_marker_byte_on_a_proxy(pause, listeners):
    caller = requests.Session()
    listeners.reset()
    stop = threading.Event()

    def flip():
        on = False
        while not stop.is_set():
            on = not on
            caller.proxies = {"http": _proxy(listeners), "https": _proxy(listeners)} if on else {}
            if pause:
                time.sleep(pause)

    with oc.environment(_env(listeners)), listeners.shim():
        exporter = otel.HTTPSpanExporter(timeout=2, session=caller)
        flipper = threading.Thread(target=flip, daemon=True)
        flipper.start()
        try:
            results = [exporter.export([oc.one_span()]) for _ in range(1000)]
        finally:
            stop.set()
            flipper.join()
            exporter.shutdown()
    listeners.settle()
    assert listeners.a.marker_bytes == 0 and listeners.b.marker_bytes == 0, listeners.a.marker_bytes + listeners.b.marker_bytes
    assert listeners.a.connections == 0 and listeners.b.connections == 0
    assert results.count(SUCCESS) == 1000, results.count(SUCCESS)
    assert listeners.target.marker_bytes > 0


# ---- the sender is judged, whoever changes it -------------------------------------------------------------------------------

def _built(listeners, session=None):
    kwargs = {"timeout": 5}
    if session is not None:
        kwargs["session"] = session
    return otel.HTTPSpanExporter(**kwargs)


def test_the_session_an_exporter_sends_with_is_not_the_one_the_caller_passed(listeners):
    caller = requests.Session()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners, caller)
        try:
            sender = otel._requests_session_of(exporter)
            assert sender is not caller and sender.trust_env is False and not sender.proxies and not any(sender.hooks.values())
        finally:
            exporter.shutdown()
            caller.close()


def test_a_sender_changed_after_the_build_to_one_that_may_use_a_proxy_sends_nothing(listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            replaced = requests.Session()
            replaced.proxies = {"http": _proxy(listeners)}
            _install_sender(exporter, replaced)
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            exporter.shutdown()
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


@pytest.mark.parametrize("change", ["proxies", "trust_env", "hook", "mount"])
def test_a_sender_changed_in_place_after_the_build_sends_nothing(change, listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            sender = otel._requests_session_of(exporter)
            if change == "proxies":
                sender.proxies = {"http": _proxy(listeners)}
            elif change == "trust_env":
                sender.trust_env = True
                with oc.environment({"HTTP_PROXY": _proxy(listeners), "ALL_PROXY": _proxy(listeners)}):
                    assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
                assert listeners.measure() == {"dialed": None, "marker_bytes": 0}
                return
            elif change == "hook":
                sender.hooks["response"].append(lambda response, **kwargs: _leak(response.request, listeners.a.port))
            else:
                sender.mount("http://", LeakyAdapter(listeners.a.port))
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            exporter.shutdown()
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


# ---- class-level patches ---------------------------------------------------------------------------------------------------

def _patched(owner, name, port):
    """A replacement for ``requests.<owner>.<name>`` that writes the request to the proxy sink at ``port`` and fails."""
    original_prepare = requests.Session.prepare_request
    original = getattr(requests.Session if owner == "Session" else requests.adapters.HTTPAdapter, name)

    def prepared(session, method, url, kwargs):
        return original_prepare(session, requests.Request(method, url, headers=kwargs.get("headers"), data=kwargs.get("data")))

    if (owner, name) == ("Session", "request"):
        return lambda self, method, url, **kwargs: _leak(prepared(self, method, url, kwargs), port)
    if (owner, name) == ("Session", "prepare_request"):
        def patched(self, request):
            _leak(original(self, request), port)
        return patched
    if (owner, name) == ("Session", "get_adapter"):
        return lambda self, url: LeakyAdapter(port)
    # Session.send, HTTPAdapter.send and HTTPAdapter.get_connection_with_tls_context all receive the prepared request first.
    return lambda self, request, *args, **kwargs: _leak(request, port)


PATCHES = [("Session", "request"), ("Session", "send"), ("Session", "prepare_request"), ("Session", "get_adapter"),
           ("HTTPAdapter", "send"), ("HTTPAdapter", "get_connection_with_tls_context")]


def _patch_class(monkeypatch, owner, name, port):
    target = requests.Session if owner == "Session" else requests.adapters.HTTPAdapter
    if not hasattr(target, name):
        pytest.skip(f"this release of requests has no {owner}.{name}")
    monkeypatch.setattr(target, name, _patched(owner, name, port))


@pytest.mark.parametrize("owner, name", PATCHES)
@pytest.mark.parametrize("own_session", [True, False])
def test_a_class_patched_before_the_build_is_refused_on_a_plain_channel(owner, name, own_session, listeners, monkeypatch):
    listeners.reset()
    caller = None if own_session else requests.Session()
    with oc.environment(_env(listeners)), listeners.shim():
        _patch_class(monkeypatch, owner, name, listeners.a.port)
        with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
            _built(listeners, caller)
    assert raised.value.code == "proxied"
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


@pytest.mark.parametrize("owner, name", PATCHES)
def test_a_class_patched_after_the_build_sends_nothing(owner, name, listeners, monkeypatch):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            _patch_class(monkeypatch, owner, name, listeners.a.port)
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            monkeypatch.undo()
            exporter.shutdown()
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_requests_own_routing_functions_are_the_ones_pinned_when_the_package_was_imported():
    for name in otel._SESSION_ROUTING:
        assert getattr(requests.Session, name).__module__.startswith("requests.")
    assert otel._session_is_stock(requests.Session())


# ---- response hooks --------------------------------------------------------------------------------------------------------

class Hook:
    def __init__(self, port):
        self.port, self.calls = port, 0

    def __call__(self, response, **kwargs):
        self.calls += 1
        _leak(response.request, self.port)


def test_a_session_with_a_response_hook_is_refused_on_a_plain_channel(listeners):
    listeners.reset()
    caller = requests.Session()
    hook = Hook(listeners.a.port)
    caller.hooks["response"].append(hook)
    with oc.environment(_env(listeners)), listeners.shim(), pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        _built(listeners, caller)
    assert raised.value.code == "proxied" and hook.calls == 0
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_a_hook_added_to_the_callers_session_after_the_build_never_runs(listeners):
    listeners.reset()
    caller = requests.Session()
    hook = Hook(listeners.a.port)
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners, caller)
        caller.hooks["response"].append(hook)
        try:
            assert exporter.export([oc.one_span()]) == SUCCESS
        finally:
            exporter.shutdown()
    listeners.settle()
    assert hook.calls == 0 and listeners.a.marker_bytes == 0 and listeners.target.marker_bytes > 0


# ---- the copy keeps the TLS settings of the session it is made from ----------------------------------------------------------

def _tls_server(tmp_path):
    """A loopback HTTPS server with a certificate for 127.0.0.1 that only the test's own CA signs."""
    key, crt = tmp_path / "key.pem", tmp_path / "crt.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(crt), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, crt


class ContextAdapter(requests.adapters.HTTPAdapter):
    """An adapter that verifies against its own CA through an ssl context it holds, as callers write them."""

    def __init__(self, context, **settings):
        self.context = context
        super().__init__(**settings)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.context
        super().init_poolmanager(*args, **kwargs)


def test_the_copy_of_a_session_keeps_the_ssl_context_of_a_custom_adapter(tmp_path):
    server, crt = _tls_server(tmp_path)
    try:
        context = ssl.create_default_context(cafile=str(crt))
        caller = requests.Session()
        caller.mount("https://", ContextAdapter(context))
        url = f"https://127.0.0.1:{server.server_address[1]}/"
        assert caller.post(url, data=b"x", timeout=3).status_code == 200
        copy = otel._copy_session(caller)
        assert copy is not caller and copy.adapters["https://"] is not caller.adapters["https://"]
        assert copy.post(url, data=b"x", timeout=3).status_code == 200
        with pytest.raises(requests.exceptions.SSLError):
            requests.Session().post(url, data=b"x", timeout=3)
    finally:
        server.shutdown()


def test_the_copy_of_a_session_keeps_verify_and_cert():
    caller = requests.Session()
    caller.verify, caller.cert = "/tmp/ca.pem", ("/tmp/c.pem", "/tmp/k.pem")
    copy = otel._copy_session(caller)
    assert copy.verify == "/tmp/ca.pem" and copy.cert == ("/tmp/c.pem", "/tmp/k.pem")


# ---- the changed caller session cannot reach the sender ----------------------------------------------------------------------

def _mutations(listeners):
    return {
        "proxies": lambda s: setattr(s, "proxies", {"http": _proxy(listeners)}),
        "an adapter mounted": lambda s: s.mount("http://", LeakyAdapter(listeners.a.port)),
        "send replaced on the object": lambda s: setattr(s, "send", lambda request, **kwargs: _leak(request, listeners.a.port)),
        "a response hook": lambda s: s.hooks["response"].append(Hook(listeners.a.port)),
    }


@pytest.mark.parametrize("name", ["proxies", "an adapter mounted", "send replaced on the object", "a response hook"])
def test_a_change_to_the_callers_session_after_the_build_does_not_fail_the_export_and_reaches_no_proxy(name, listeners):
    listeners.reset()
    caller = requests.Session()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners, caller)
        _mutations(listeners)[name](caller)
        try:
            assert exporter.export([oc.one_span()]) == SUCCESS
        finally:
            exporter.shutdown()
    listeners.settle()
    assert listeners.a.marker_bytes == 0 and listeners.a.connections == 0 and listeners.target.marker_bytes > 0

