"""On a plain channel that carries Future AGI keys the sender's adapters are built from plain settings, and the sender is judged
again inside the guarded send.

The adapter copy is a new ``requests.adapters.HTTPAdapter`` made from the pool sizes and from ``max_retries`` rebuilt as exactly
``urllib3.util.Retry`` (a ``Retry`` subclass is rebuilt as a plain ``Retry`` with the same field values); nothing the caller's
adapter holds as an instance attribute is carried over. Marker key bytes are counted at the target listener and at two
CONNECT-aware proxy sinks, on every installed OpenTelemetry release.
"""
import http.cookiejar
import http.server
import socket
import threading
from unittest import mock

import pytest
import requests
import urllib3
from opentelemetry.sdk.trace.export import SpanExportResult
from urllib3.util import Retry

import oracle_conformance as oc
from fi_instrumentation import otel
from test_plain_channel_isolated_sender import _built, _proxy
from test_plaintext_post_build_session import _env

SUCCESS, FAILURE = SpanExportResult.SUCCESS, SpanExportResult.FAILURE


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


def _outcome(exporter):
    """The result of one export, or the name of the exception it raised."""
    try:
        return exporter.export([oc.one_span()])
    except Exception as error:  # the transport of some releases raises when the collector is unreachable
        return type(error).__name__


def _sender_adapter(exporter):
    return otel._requests_session_of(exporter).adapters["http://"]


class _Closer:
    """A loopback listener that reads a request and closes the connection without answering."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            try:
                conn.settimeout(0.5)
                conn.recv(65536)
            except OSError:
                pass
            conn.close()

    def close(self):
        self.sock.close()


def _shim_to(listeners, port):
    """Dials to either proxy sink go to it; every other dial goes to ``port``."""
    import urllib3.util.connection as connection

    real = connection.create_connection
    proxies = {listeners.a.port, listeners.b.port}

    def create_connection(address, *args, **kwargs):
        host, dialed = address
        if host in ("127.0.0.1", "localhost") and dialed in proxies:
            return real(("127.0.0.1", dialed), *args, **kwargs)
        return real(("127.0.0.1", port), *args, **kwargs)

    return mock.patch.object(connection, "create_connection", create_connection)


def _flipping_after_the_first_check(change):
    """Run ``change`` on the sender right after the first judgement of it (the one ``export`` makes), before the send."""
    real = otel._sender_is_sound
    state = {"calls": 0}

    def judged(sender, *args, **kwargs):
        verdict = real(sender, *args, **kwargs)
        state["calls"] += 1
        if state["calls"] == 1:
            change(sender)
        return verdict

    return mock.patch.object(otel, "_sender_is_sound", judged)


def _bytes_at_proxies(listeners):
    listeners.settle()
    return listeners.a.marker_bytes + listeners.b.marker_bytes


# ---- F1: a Retry subclass --------------------------------------------------------------------------------------------------

def _retry_that_redirects(port):
    class Redirecting(Retry):
        def increment(self, *args, **kwargs):
            pool = kwargs.get("_pool")
            if pool is not None:
                pool.host, pool.port = "127.0.0.1", port
            return super().increment(*args, **kwargs)

    return Redirecting(total=3, connect=3, read=3, other=3, allowed_methods=None)


def test_a_retry_subclass_that_moves_the_pool_to_a_proxy_puts_no_marker_byte_on_the_proxy(listeners):
    closer = _Closer()
    caller = requests.Session()
    caller.mount("http://", requests.adapters.HTTPAdapter(max_retries=_retry_that_redirects(listeners.a.port)))
    listeners.reset()
    try:
        with oc.environment(_env(listeners)), _shim_to(listeners, closer.port):
            exporter = _built(listeners, caller)
            try:
                _outcome(exporter)
            finally:
                exporter.shutdown()
    finally:
        closer.close()
    assert _bytes_at_proxies(listeners) == 0 and listeners.a.connections == 0 and listeners.b.connections == 0
    assert closer.connections > 0


def test_the_retry_of_the_sending_adapter_is_exactly_retry_with_the_values_of_the_callers():
    class Custom(Retry):
        pass

    wanted = dict(total=5, connect=4, read=3, redirect=2, status=6, other=7, backoff_factor=0.25, allowed_methods=None,
                  status_forcelist=[502, 503], raise_on_redirect=False, raise_on_status=False)
    caller = requests.Session()
    caller.mount("http://", requests.adapters.HTTPAdapter(max_retries=Custom(**wanted)))
    sender = otel._isolated_session(caller)
    retries = sender.adapters["http://"].max_retries
    assert type(retries) is Retry
    for name, value in wanted.items():
        held = getattr(retries, name)
        assert (set(held) == set(value) if name == "status_forcelist" else held == value), name


@pytest.mark.parametrize("retries", [0, 3, Retry(total=2, backoff_factor=0.5, status_forcelist=[503])], ids=["0", "int", "Retry"])
def test_an_int_or_a_stock_retry_is_kept(retries):
    caller = requests.Session()
    caller.mount("http://", requests.adapters.HTTPAdapter(max_retries=retries))
    held = caller.adapters["http://"].max_retries
    sender = otel._isolated_session(caller)
    copied = sender.adapters["http://"].max_retries
    assert type(copied) is Retry
    assert copied.total == held.total and copied.read == held.read and copied.backoff_factor == held.backoff_factor
    assert set(copied.status_forcelist or ()) == set(held.status_forcelist or ())


def test_the_pool_sizes_and_blocking_of_an_adapter_are_kept():
    caller = requests.Session()
    caller.mount("http://", requests.adapters.HTTPAdapter(pool_connections=3, pool_maxsize=7, pool_block=True))
    adapter = otel._isolated_session(caller).adapters["http://"]
    assert (adapter._pool_connections, adapter._pool_maxsize, adapter._pool_block) == (3, 7, True)
    assert type(adapter) is requests.adapters.HTTPAdapter and adapter is not caller.adapters["http://"]


def test_every_mounted_prefix_gets_its_own_new_adapter():
    caller = requests.Session()
    caller.mount("http://special.example/", requests.adapters.HTTPAdapter(pool_maxsize=2))
    sender = otel._isolated_session(caller)
    assert set(sender.adapters) == set(caller.adapters)
    for prefix, adapter in caller.adapters.items():
        assert sender.adapters[prefix] is not adapter and type(sender.adapters[prefix]) is requests.adapters.HTTPAdapter


# ---- F2: attributes an adapter holds on the instance ---------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(requests.adapters.HTTPAdapter, "build_connection_pool_key_attributes"), reason="not in this requests")
def test_an_instance_pool_key_function_on_a_stock_adapter_puts_no_marker_byte_on_the_proxy(listeners):
    caller = requests.Session()
    adapter = requests.adapters.HTTPAdapter()
    port = listeners.a.port

    def key_attributes(request, verify, cert=None):
        return {"scheme": "http", "host": "127.0.0.1", "port": port}, {}

    adapter.build_connection_pool_key_attributes = key_attributes
    caller.mount("http://", adapter)
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        try:
            exporter = _built(listeners, caller)
        except otel.UnsupportedFutureAGIEndpointError as refused:
            assert refused.code == "proxied"
        else:
            try:
                _outcome(exporter)
                assert "build_connection_pool_key_attributes" not in vars(_sender_adapter(exporter))
            finally:
                exporter.shutdown()
    assert _bytes_at_proxies(listeners) == 0 and listeners.a.connections == 0


def test_the_sending_adapter_carries_no_instance_attribute_of_the_callers():
    caller = requests.Session()
    adapter = requests.adapters.HTTPAdapter()
    adapter.marker_attribute = object()
    adapter.poolmanager = urllib3.ProxyManager("http://127.0.0.1:9")
    caller.mount("http://", adapter)
    held = otel._isolated_session(caller).adapters["http://"]
    assert "marker_attribute" not in vars(held)
    assert type(held.poolmanager) is urllib3.PoolManager


# ---- F3: the sender is judged again inside the guarded send -----------------------------------------------------------------

def test_a_proxy_set_between_the_export_check_and_the_send_puts_no_marker_byte_on_the_proxy(listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            with _flipping_after_the_first_check(lambda sender: setattr(sender, "proxies", {"http": _proxy(listeners)})):
                result = _outcome(exporter)
        finally:
            exporter.shutdown()
    assert result == FAILURE
    assert _bytes_at_proxies(listeners) == 0 and listeners.a.connections == 0
    assert listeners.target.marker_bytes == 0


def test_an_environment_proxy_turned_on_between_the_export_check_and_the_send_fails_the_export(listeners):
    listeners.reset()
    with oc.environment({**_env(listeners), "HTTP_PROXY": _proxy(listeners)}), listeners.shim():
        exporter = _built(listeners)
        try:
            with _flipping_after_the_first_check(lambda sender: setattr(sender, "trust_env", True)):
                result = _outcome(exporter)
        finally:
            exporter.shutdown()
    assert result == FAILURE
    assert _bytes_at_proxies(listeners) == 0 and listeners.target.marker_bytes == 0


def test_a_pool_manager_swapped_to_a_proxy_manager_after_the_build_fails_the_export_with_no_bytes(listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            _sender_adapter(exporter).poolmanager = urllib3.ProxyManager(_proxy(listeners))
            result = _outcome(exporter)
        finally:
            exporter.shutdown()
    assert result == FAILURE
    assert _bytes_at_proxies(listeners) == 0 and listeners.a.connections == 0 and listeners.target.marker_bytes == 0


def test_a_proxy_manager_entry_on_a_mounted_adapter_fails_the_export(listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            _sender_adapter(exporter).proxy_manager["http://127.0.0.1:1"] = urllib3.PoolManager()
            result = _outcome(exporter)
        finally:
            exporter.shutdown()
    assert result == FAILURE and listeners.target.marker_bytes == 0


def test_a_sender_that_is_untouched_still_sends(listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            assert _outcome(exporter) == SUCCESS
        finally:
            exporter.shutdown()
    assert _bytes_at_proxies(listeners) == 0 and listeners.target.marker_bytes > 0


# ---- F6: each routing name is in the checked set ---------------------------------------------------------------------------

SESSION_NAMES = ["request", "send", "prepare_request", "get_adapter", "merge_environment_settings", "rebuild_proxies"]
ADAPTER_NAMES = ["send", "get_connection", "get_connection_with_tls_context", "proxy_manager_for", "init_poolmanager"]


def _override(owner, name):
    original = getattr(owner, name)

    def replaced(*args, **kwargs):
        return original(*args, **kwargs)

    setattr(owner, name, replaced)


@pytest.mark.parametrize("name", SESSION_NAMES)
def test_a_routing_function_replaced_on_a_session_object_is_refused(name, listeners):
    caller = requests.Session()
    _override(caller, name)
    with oc.environment(_env(listeners)), listeners.shim(), pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        _built(listeners, caller)
    assert raised.value.code == "proxied"


@pytest.mark.parametrize("name", ADAPTER_NAMES)
def test_a_routing_function_replaced_on_an_adapter_object_is_refused(name, listeners):
    caller = requests.Session()
    adapter = requests.adapters.HTTPAdapter()
    _override(adapter, name)
    caller.mount("http://", adapter)
    with oc.environment(_env(listeners)), listeners.shim(), pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        _built(listeners, caller)
    assert raised.value.code == "proxied"


@pytest.mark.parametrize("name", SESSION_NAMES)
def test_the_session_check_fails_when_a_name_is_dropped_from_the_session_routing_names(name, monkeypatch):
    caller = requests.Session()
    _override(caller, name)
    assert otel._session_is_stock(caller) is False
    monkeypatch.setattr(otel, "_SESSION_ROUTING", tuple(n for n in otel._SESSION_ROUTING if n != name))
    assert otel._session_is_stock(caller) is True  # the mutation is visible: the named check is what refuses it


@pytest.mark.parametrize("name", ADAPTER_NAMES)
def test_the_session_check_fails_when_a_name_is_dropped_from_the_adapter_routing_names(name, monkeypatch):
    caller = requests.Session()
    adapter = requests.adapters.HTTPAdapter()
    _override(adapter, name)
    caller.mount("http://", adapter)
    assert otel._session_is_stock(caller) is False
    monkeypatch.setattr(otel, "_ADAPTER_ROUTING", tuple(n for n in otel._ADAPTER_ROUTING if n != name))
    assert otel._session_is_stock(caller) is True


def test_the_checked_routing_names_are_the_ones_listed_here():
    assert set(otel._SESSION_ROUTING) >= set(SESSION_NAMES) and set(otel._ADAPTER_ROUTING) >= set(ADAPTER_NAMES)


# ---- nits -----------------------------------------------------------------------------------------------------------------

def _redirecting_target(location):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_an_export_answered_with_a_redirect_returns_failure_and_follows_nothing(listeners):
    listeners.reset()
    server = _redirecting_target(f"http://127.0.0.1:{listeners.a.port}/v1/traces")
    try:
        with oc.environment(_env(listeners)), _shim_to(listeners, server.server_address[1]):
            exporter = _built(listeners)
            try:
                result = _outcome(exporter)
            finally:
                exporter.shutdown()
    finally:
        server.shutdown()
    assert result == FAILURE
    assert listeners.a.connections == 0 and _bytes_at_proxies(listeners) == 0


def test_an_auth_held_in_a_list_is_refused_without_a_value(listeners):
    caller = requests.Session()
    caller.auth = ["user", "password"]
    with oc.environment(_env(listeners)), listeners.shim(), pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        _built(listeners, caller)
    assert raised.value.code == "proxied" and "password" not in str(raised.value)
    assert otel._auth_is_stock(("user", "password")) is True and otel._auth_is_stock(["user", "password"]) is False


def test_a_standard_library_cookie_jar_is_copied_to_the_sender(listeners):
    caller = requests.Session()
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(http.cookiejar.Cookie(
        0, "sid", "abc", None, False, "127.0.0.1", True, False, "/", True, False, None, False, None, None, {},
    ))
    caller.cookies = jar
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners, caller)
        try:
            held = otel._requests_session_of(exporter).cookies
            assert [cookie.name for cookie in held] == ["sid"] and held is not jar
        finally:
            exporter.shutdown()
