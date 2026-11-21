"""On a plain channel that carries Future AGI keys every adapter mounted on the session is judged by its exact type.

Only ``requests.adapters.HTTPAdapter`` itself is sent through: a subclass can choose the connection it uses (a pool key, a
dial, an ssl context), so a plain channel with one is refused with code ``proxied`` before anything is sent, and an export
whose sender gained one sends nothing. Marker key bytes are counted at the target listener and at two CONNECT-aware proxy
sinks. The same cases run on every installed OpenTelemetry release (the CI matrix runs 1.29.0 and 1.45.2).
"""
import socket

import pytest
import requests
from opentelemetry.sdk.trace.export import SpanExportResult

import oracle_conformance as oc
from fi_instrumentation import otel
from test_plain_channel_isolated_sender import Hook, _built, _install_sender, _proxy, _tls_server
from test_plaintext_post_build_session import _env

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


# ---- adapter classes as callers write them -----------------------------------------------------------------------------------

def _adapter_classes(port):
    class PoolKeyRewriter(requests.adapters.HTTPAdapter):
        def build_connection_pool_key_attributes(self, request, verify, cert=None):
            host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
            host_params = dict(host_params, host="127.0.0.1", port=port)
            return host_params, pool_kwargs

    class TlsOnly(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            import ssl

            kwargs["ssl_context"] = ssl.create_default_context()
            super().init_poolmanager(*args, **kwargs)

    class TCPKeepAliveAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["socket_options"] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
            super().init_poolmanager(*args, **kwargs)

    class SourceAddressAdapter(requests.adapters.HTTPAdapter):
        def __init__(self, source_address, **kwargs):
            self.source_address = source_address
            super().__init__(**kwargs)

        def init_poolmanager(self, connections, maxsize, block=False):
            super().init_poolmanager(connections, maxsize, block, source_address=self.source_address)

    class HostHeaderSSLAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            request.headers.setdefault("Host", "example.com")
            return super().send(request, **kwargs)

    class NoOverrides(requests.adapters.HTTPAdapter):
        pass

    return {
        "build_connection_pool_key_attributes": PoolKeyRewriter(),
        "ssl context in init_poolmanager": TlsOnly(),
        "TCPKeepAliveAdapter": TCPKeepAliveAdapter(),
        "SourceAddressAdapter": SourceAddressAdapter(("127.0.0.1", 0)),
        "HostHeaderSSLAdapter": HostHeaderSSLAdapter(),
        "no overrides": NoOverrides(),
    }


NAMES = ["build_connection_pool_key_attributes", "ssl context in init_poolmanager", "TCPKeepAliveAdapter", "SourceAddressAdapter",
         "HostHeaderSSLAdapter", "no overrides"]


@pytest.mark.parametrize("name", NAMES)
def test_a_session_with_an_adapter_subclass_is_refused_on_a_plain_channel(name, listeners):
    listeners.reset()
    caller = requests.Session()
    caller.mount("http://", _adapter_classes(listeners.a.port)[name])
    with oc.environment(_env(listeners)), listeners.shim(), pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        _built(listeners, caller)
    assert raised.value.code == "proxied"
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


@pytest.mark.parametrize("name", NAMES)
def test_a_subclass_mounted_on_the_sender_after_the_build_sends_nothing(name, listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            otel._requests_session_of(exporter).mount("http://", _adapter_classes(listeners.a.port)[name])
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            exporter.shutdown()
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_a_session_with_requests_own_adapter_and_its_settings_is_sent_through(listeners):
    listeners.reset()
    caller = requests.Session()
    caller.mount("http://", requests.adapters.HTTPAdapter(max_retries=3, pool_connections=2, pool_maxsize=3))
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners, caller)
        try:
            assert exporter.export([oc.one_span()]) == SUCCESS
        finally:
            exporter.shutdown()
    listeners.settle()
    assert listeners.a.marker_bytes == 0 and listeners.b.marker_bytes == 0 and listeners.target.marker_bytes > 0


def test_an_adapter_subclass_on_a_channel_with_tls_is_sent_through_and_verifies(tmp_path, listeners):
    server, crt = _tls_server(tmp_path)
    class ContextAdapter(requests.adapters.HTTPAdapter):
        """A subclass that adds a keep-alive socket option and nothing else."""

        def init_poolmanager(self, *args, **kwargs):
            kwargs["socket_options"] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
            super().init_poolmanager(*args, **kwargs)

    caller = requests.Session()
    caller.mount("https://", ContextAdapter())
    try:
        with oc.environment(_env(listeners)):
            exporter = otel.HTTPSpanExporter(endpoint=f"https://127.0.0.1:{server.server_address[1]}/v1/traces", timeout=5, session=caller,
                                            certificate_file=str(crt))
            try:
                assert exporter.export([oc.one_span()]) == SUCCESS
            finally:
                exporter.shutdown()
        other = otel.HTTPSpanExporter(endpoint=f"https://127.0.0.1:{server.server_address[1]}/v1/traces", timeout=5)
        try:
            with oc.environment(_env(listeners)):
                try:
                    assert other.export([oc.one_span()]) == SpanExportResult.FAILURE
                except requests.exceptions.SSLError:  # a release that does not catch it
                    pass
        finally:
            other.shutdown()
    finally:
        server.shutdown()


# ---- the hooks of the sender and the redirect guard are read directly ------------------------------------------------------------

class HidingHooks(dict):
    """A dict whose values() shows nothing."""

    def values(self):
        return []


def test_hooks_held_in_a_dict_subclass_that_hides_its_values_send_nothing(listeners):
    listeners.reset()
    hook = Hook(listeners.a.port)
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            sender = otel._requests_session_of(exporter)
            sender.hooks = HidingHooks(response=[hook])
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            exporter.shutdown()
    assert hook.calls == 0
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_a_response_hook_on_a_plain_dict_of_hooks_is_judged_by_direct_iteration():
    sender = requests.Session()
    assert otel._session_is_stock(sender)
    sender.hooks["response"].append(lambda response, **kwargs: None)
    assert not otel._session_is_stock(sender)
    sender.hooks = HidingHooks(response=[lambda response, **kwargs: None])
    assert not otel._session_is_stock(sender)


@pytest.mark.parametrize("change", ["removed", "replaced"])
def test_a_redirect_guard_removed_or_replaced_on_the_sender_sends_nothing(change, listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            sender = otel._requests_session_of(exporter)
            if change == "removed":
                del sender.__dict__["request"]
            else:
                inner = sender.request
                sender.__dict__["request"] = lambda method, url, **kwargs: inner(method, url, **kwargs)
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            exporter.shutdown()
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


# ---- an APM that wraps urllib3 or the socket by calling through is not refused --------------------------------------------------

def _wrap(monkeypatch, owner, name):
    original = getattr(owner, name)

    def wrapped(*args, **kwargs):
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, staticmethod(wrapped) if owner is socket else _method(original))


def _method(original):
    def wrapped(self, *args, **kwargs):
        return original(self, *args, **kwargs)

    return wrapped


@pytest.mark.parametrize("where", ["PoolManager.urlopen", "HTTPConnectionPool.urlopen", "socket.create_connection"])
def test_a_plain_export_is_sent_when_an_apm_wraps_urllib3_or_the_socket(where, listeners, monkeypatch):
    import urllib3

    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        if where == "socket.create_connection":
            _wrap(monkeypatch, socket, "create_connection")
        else:
            owner, name = where.split(".")
            _wrap(monkeypatch, getattr(urllib3, owner), name)
        exporter = _built(listeners)
        try:
            assert exporter.export([oc.one_span()]) == SUCCESS
        finally:
            exporter.shutdown()
    listeners.settle()
    assert listeners.a.marker_bytes == 0 and listeners.b.marker_bytes == 0 and listeners.target.marker_bytes > 0
