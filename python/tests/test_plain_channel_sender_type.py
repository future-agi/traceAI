"""On a plain channel that carries Future AGI keys the sender is always ``requests.Session`` itself, and every container it holds is
read as the plain type ``requests`` uses.

A ``requests.Session`` subclass passed as ``session=`` has none of its own methods run to send (a ``post`` that rewrites the
URL or adds a header): the isolated sender is built as ``requests.Session()`` and carries over only the settings that are
inspected. On a channel with TLS the session the caller passed is used as built. The ``adapters`` and ``proxies`` of the sender are
read as a ``dict`` or ``OrderedDict`` by direct iteration: a subclass that shows less than it holds fails the export. Marker key
bytes are counted at the target listener and at two CONNECT-aware proxy sinks. The same cases run on every installed OpenTelemetry
release (the CI matrix runs 1.29.0 and 1.45.2).
"""
import collections
import pathlib

import pytest
import requests
from opentelemetry.sdk.trace.export import SpanExportResult

import oracle_conformance as oc
from fi_instrumentation import otel
from test_plain_channel_adapters import _adapter_classes
from test_plain_channel_isolated_sender import _built, _proxy, _tls_server
from test_plaintext_post_build_session import _env

SUCCESS = SpanExportResult.SUCCESS
VERBS = ["post", "get", "put", "delete", "head", "patch", "options"]


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


def _subclass(verb, port=None):
    """A session class whose ``verb`` method counts its calls and, with a ``port``, sends the request to that proxy sink instead."""

    class Verbose(requests.Session):
        calls = 0

    def method(self, url, *args, **kwargs):
        type(self).calls += 1
        if port is not None:
            url = f"http://127.0.0.1:{port}/v1/traces"
        return getattr(super(Verbose, self), verb)(url, *args, **kwargs)

    setattr(Verbose, verb, method)
    return Verbose


# ---- a caller's session subclass is not used to send on a plain channel -------------------------------------------------------------

@pytest.mark.parametrize("rewrite", [True, False], ids=["rewrites the url to a proxy", "adds nothing"])
@pytest.mark.parametrize("verb", VERBS)
def test_a_session_subclass_overriding_a_verb_is_not_used_to_send_on_a_plain_channel(verb, rewrite, listeners):
    listeners.reset()
    cls = _subclass(verb, listeners.a.port if rewrite else None)
    caller = cls()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners, caller)
        try:
            sender = otel._requests_session_of(exporter)
            assert type(sender) is requests.Session
            assert exporter.export([oc.one_span()]) == SUCCESS
        finally:
            exporter.shutdown()
    listeners.settle()
    assert cls.calls == 0
    assert listeners.a.marker_bytes == 0 and listeners.b.marker_bytes == 0 and listeners.a.connections == 0
    assert listeners.target.marker_bytes > 0


def test_a_session_subclass_that_overrides_post_and_needs_arguments_is_not_used_either(listeners):
    listeners.reset()

    class Arguments(requests.Session):
        calls = 0

        def __init__(self, port):
            super().__init__()
            self.port = port

        def post(self, url, *args, **kwargs):
            type(self).calls += 1
            return super().post(f"http://127.0.0.1:{self.port}/v1/traces", *args, **kwargs)

    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners, Arguments(listeners.a.port))
        try:
            assert type(otel._requests_session_of(exporter)) is requests.Session
            assert exporter.export([oc.one_span()]) == SUCCESS
        finally:
            exporter.shutdown()
    listeners.settle()
    assert Arguments.calls == 0 and listeners.a.marker_bytes == 0 and listeners.target.marker_bytes > 0


def test_a_session_subclass_with_a_routing_override_is_still_refused(listeners):
    class Routing(requests.Session):
        def get_adapter(self, url):
            return super().get_adapter(url)

    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim(), pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        _built(listeners, Routing())
    assert raised.value.code == "proxied"
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_the_isolated_sender_is_requests_session_and_keeps_the_inspected_settings():
    class Custom(requests.Session):
        pass

    caller = Custom()
    caller.verify = "/etc/ssl/certs/ca-certificates.crt"
    caller.cert = ("client.crt", "client.key")
    caller.headers["X-Team"] = "traces"
    caller.max_redirects = 3
    caller.auth = ("user", "password")
    caller.mount("http://", requests.adapters.HTTPAdapter(max_retries=4, pool_connections=2, pool_maxsize=5))
    sender = otel._isolated_session(caller)
    assert type(sender) is requests.Session
    assert sender.verify == caller.verify and sender.cert == caller.cert and sender.max_redirects == 3 and sender.auth == ("user", "password")
    assert sender.headers["X-Team"] == "traces"
    adapter = sender.adapters["http://"]
    assert type(adapter) is requests.adapters.HTTPAdapter and adapter is not caller.adapters["http://"]
    assert adapter.max_retries.total == 4 and adapter._pool_connections == 2 and adapter._pool_maxsize == 5
    assert sender.trust_env is False and sender.proxies == {} and not any(sender.hooks.values())


def test_a_session_subclass_is_used_as_built_on_a_channel_with_tls(tmp_path, listeners):
    server, crt = _tls_server(tmp_path)

    class Counting(requests.Session):
        calls = 0

        def post(self, *args, **kwargs):
            type(self).calls += 1
            return super().post(*args, **kwargs)

    caller = Counting()
    try:
        with oc.environment(_env(listeners)):
            exporter = otel.HTTPSpanExporter(endpoint=f"https://127.0.0.1:{server.server_address[1]}/v1/traces", timeout=5, session=caller,
                                            certificate_file=str(crt))
            try:
                assert exporter.export([oc.one_span()]) == SUCCESS
                assert isinstance(otel._requests_session_of(exporter), Counting)
            finally:
                exporter.shutdown()
        if hasattr(exporter, "_session"):
            assert Counting.calls > 0
    finally:
        server.shutdown()


# ---- the sender is judged by its exact type on every export -----------------------------------------------------------------

def test_a_sender_whose_class_is_changed_to_a_subclass_after_the_build_sends_nothing(listeners):
    listeners.reset()
    cls = _subclass("post", listeners.a.port)
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            otel._requests_session_of(exporter).__class__ = cls
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            exporter.shutdown()
    assert cls.calls == 0
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_a_session_of_a_subclass_is_not_a_sound_sender():
    sender = otel._isolated_session(requests.Session())
    guard = otel._refuse_redirects(sender)
    assert otel._sender_is_sound(sender, guard)
    sender.__class__ = type("Sub", (requests.Session,), {})
    assert not otel._sender_is_sound(sender, guard)


# ---- adapters and proxies are read as the plain types requests uses -----------------------------------------------------------

class HidingAdapters(collections.OrderedDict):
    """An ordered dict whose values() shows nothing, while iteration by items() shows what it holds."""

    def values(self):
        return []


class FalsyProxies(dict):
    """A dict that holds entries and reads as empty."""

    def __bool__(self):
        return False


class EmptyLengthProxies(dict):
    def __len__(self):
        return 0


def test_adapters_in_a_dict_subclass_that_hides_its_values_send_nothing(listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            sender = otel._requests_session_of(exporter)
            sender.adapters = HidingAdapters([("http://", _adapter_classes(listeners.a.port)["build_connection_pool_key_attributes"])])
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            exporter.shutdown()
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_adapters_are_judged_by_direct_iteration_and_a_plain_dict_or_ordered_dict_is_accepted():
    sender = requests.Session()
    assert otel._session_is_stock(sender)
    sender.adapters = dict(sender.adapters)
    assert otel._session_is_stock(sender)
    sender.adapters = collections.OrderedDict(sender.adapters)
    assert otel._session_is_stock(sender)
    sender.adapters = HidingAdapters(sender.adapters)
    assert not otel._session_is_stock(sender)


@pytest.mark.parametrize("proxies", [FalsyProxies, EmptyLengthProxies], ids=["falsy", "empty length"])
def test_proxies_in_a_dict_subclass_that_reads_as_empty_send_nothing(proxies, listeners):
    listeners.reset()
    with oc.environment(_env(listeners)), listeners.shim():
        exporter = _built(listeners)
        try:
            sender = otel._requests_session_of(exporter)
            sender.proxies = proxies(http=_proxy(listeners), https=_proxy(listeners))
            assert exporter.export([oc.one_span()]) == SpanExportResult.FAILURE
        finally:
            exporter.shutdown()
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


def test_proxies_are_judged_as_a_plain_dict_that_holds_nothing():
    sender = otel._isolated_session(requests.Session())
    guard = otel._refuse_redirects(sender)
    assert otel._sender_is_sound(sender, guard)
    sender.proxies = FalsyProxies()
    assert not otel._sender_is_sound(sender, guard)
    sender.proxies = {"http": "http://127.0.0.1:9"}
    assert not otel._sender_is_sound(sender, guard)
    sender.proxies = {}
    assert otel._sender_is_sound(sender, guard)


# ---- the changelog states the limits as breaks with their user action ---------------------------------------------------------

def _bullets():
    text = (pathlib.Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
    start = text.index("## [1.5.2]")
    section = text[start : text.index("\n## [", start + 1)]
    return [line for line in section.splitlines() if line.startswith("- ")]


def _after_label(bullet):
    return bullet.split("Breaking:", 1)[1] if "Breaking:" in bullet else ""


def test_the_adapter_subclass_break_is_labelled_and_names_the_user_action():
    labelled = [b for b in _bullets() if "HTTPAdapter" in _after_label(b) and "subclass" in _after_label(b)]
    assert len(labelled) == 1
    action = _after_label(labelled[0])
    assert "`proxied`" in action and "use https" in action and "mount `requests.adapters.HTTPAdapter` itself" in action


def test_the_adapter_break_names_the_libraries_that_mount_a_subclass_and_the_plain_session_to_use():
    labelled = [b for b in _bullets() if "HTTPAdapter" in _after_label(b) and "subclass" in _after_label(b)]
    text = _after_label(labelled[0])
    for name in ("requests_futures.FuturesSession", "TCPKeepAliveAdapter", "SourceAddressAdapter", "HostHeaderSSLAdapter", "init_poolmanager",
                 "requests.Session"):
        assert name in text, name


def test_the_session_subclass_break_is_labelled_and_names_the_user_action():
    labelled = [b for b in _bullets() if "Session` subclass" in _after_label(b)]
    assert len(labelled) == 1
    text = _after_label(labelled[0])
    assert "`post`" in text and "plain-http" in text and "headers" in text and "use https" in text
