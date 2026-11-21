"""A requests session the caller passes to the HTTP exporter is judged again on every export, not only when the exporter is built.

Each case builds the exporter with a session that nothing routes (a plain channel to ``example.com``, which a shim sends to
the target listener), changes the session or the environment, and exports. Marker key bytes are counted at the target listener
and at two CONNECT-aware proxy sinks: a refused export puts none on any of them. The same cases run on every installed
OpenTelemetry release (the CI matrix runs 1.29.0 and 1.45.2).
"""
import contextlib
import os
import socket
from unittest import mock

import pytest
import requests
from opentelemetry.sdk.trace.export import SpanExportResult

import oracle_conformance as oc
from fi_instrumentation import otel

OPT_IN = "FI_ALLOW_INSECURE_HTTP"
BASE = "http://example.com:{T}/"


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


def _leak(request, port):
    """What a caller's own transport might do: write the whole prepared request to a proxy sink."""
    head = f"{request.method} {request.url} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in request.headers.items())
    body = request.body or b""
    if isinstance(body, str):
        body = body.encode()
    with contextlib.suppress(OSError):
        with socket.create_connection(("127.0.0.1", port), timeout=1) as conn:
            conn.sendall(head.encode("latin-1", "replace") + b"\r\n" + body)
            conn.settimeout(0.2)
            with contextlib.suppress(OSError):
                conn.recv(100)
    raise requests.exceptions.ConnectionError("leaky transport")


class LeakyAdapter(requests.adapters.BaseAdapter):
    port = None

    def __init__(self, port=None, **settings):
        super().__init__()
        self.port = port if port is not None else type(self).port

    def send(self, request, **kwargs):
        _leak(request, self.port)

    def close(self):
        return None


def _change_proxies(session, ports):
    session.proxies = {"http": f"http://127.0.0.1:{ports['A']}", "https": f"http://127.0.0.1:{ports['A']}"}


def _change_environment(session, ports):
    # The environment changes after the build; ``trust_env`` of the caller's session is on.
    os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{ports['A']}"
    os.environ["ALL_PROXY"] = f"http://127.0.0.1:{ports['A']}"


def _mount_adapter(session, ports):
    session.mount("http://", LeakyAdapter(ports["A"]))


def _patch_session_send(session, ports):
    session.send = lambda request, **kwargs: _leak(request, ports["A"])


def _patch_adapter_send(session, ports):
    adapter = session.get_adapter("http://example.com/")
    adapter.send = lambda request, **kwargs: _leak(request, ports["A"])


def _auth_hook(session, ports):
    def rewrite(request):
        request.url = f"http://127.0.0.1:{ports['A']}/"
        return request

    session.auth = rewrite


CHANGES = {
    "session.proxies": _change_proxies,
    "environment proxy variables": _change_environment,
    "an adapter mounted": _mount_adapter,
    "instance-patched session.send": _patch_session_send,
    "instance-patched adapter.send": _patch_adapter_send,
    "a session.auth hook that rewrites the URL": _auth_hook,
}


def _env(listeners):
    values = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    values.update({"FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET, OPT_IN: "1",
                   "FI_BASE_URL": BASE.replace("{T}", str(listeners.target.port))})
    return values


def _export(listeners, change, session=True, before_build=False):
    """Build, change, export. Returns (error code at build, export result, measured, counters of the sinks)."""
    ports = {"A": listeners.a.port, "B": listeners.b.port, "T": listeners.target.port}
    caller = requests.Session() if session else None
    listeners.reset()
    code = result = None
    saved = {name: os.environ.get(name) for name in ("HTTP_PROXY", "ALL_PROXY")}
    try:
        with oc.environment(_env(listeners)), listeners.shim():
            if before_build and change is not None:
                change(caller, ports)
            try:
                exporter = otel.HTTPSpanExporter(timeout=0.5, **({"session": caller} if caller is not None else {}))
            except otel.UnsupportedFutureAGIEndpointError as error:
                return error.code, None, listeners.measure(), (listeners.a.connections, listeners.b.connections)
            try:
                if change is not None and not before_build:
                    change(caller if caller is not None else otel._requests_session_of(exporter), ports)
                with contextlib.suppress(requests.exceptions.RequestException):
                    result = exporter.export([oc.one_span()])
            finally:
                exporter.shutdown()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if caller is not None:
            caller.close()
    return code, result, listeners.measure(), (listeners.a.connections, listeners.b.connections)


@pytest.mark.parametrize("name", list(CHANGES))
def test_a_session_changed_after_the_build_is_refused_on_export_and_no_marker_byte_reaches_any_sink(name, listeners):
    code, result, measured, (a, b) = _export(listeners, CHANGES[name])
    assert code is None
    assert result == SpanExportResult.FAILURE
    assert measured == {"dialed": None, "marker_bytes": 0}
    assert a == 0 and b == 0


@pytest.mark.parametrize("name", list(CHANGES))
def test_the_same_change_made_before_the_build_is_refused_when_the_exporter_is_built(name, listeners):
    if name == "environment proxy variables":
        pytest.skip("the environment is read when the exporter is built: covered by the oracle rows")
    code, result, measured, (a, b) = _export(listeners, CHANGES[name], before_build=True)
    assert code == "proxied"
    assert measured == {"dialed": None, "marker_bytes": 0}
    assert a == 0 and b == 0


@pytest.mark.parametrize("name", list(CHANGES))
def test_with_the_judgement_taken_out_the_changed_session_puts_marker_bytes_on_a_sink(name, listeners):
    with mock.patch.object(otel, "_session_may_leave_its_route", lambda *a, **k: False):
        code, result, measured, (a, b) = _export(listeners, CHANGES[name])
    assert code is None
    assert measured["marker_bytes"] > 0


def test_a_session_nothing_changed_is_used_as_built(listeners):
    code, result, measured, (a, b) = _export(listeners, None)
    assert code is None
    assert measured["dialed"] == "target" and measured["marker_bytes"] > 0 and a == 0 and b == 0


def test_a_session_this_package_builds_is_unchanged_by_an_environment_proxy_set_after_the_build(listeners):
    code, result, measured, (a, b) = _export(listeners, _change_environment, session=False)
    assert code is None
    assert measured["dialed"] == "target" and measured["marker_bytes"] > 0
    assert a == 0 and b == 0


# ---- key header names written with an encoded delimiter ------------------------------------------------------------------

ENCODED_FORMS = {
    "name and value joined by an encoded equals sign": "x-api-key%3D{key}",
    "a header whose value carries an encoded pair": "a=b%2Cx-api-key%3D{key}",
    "an encoded pair after a plain header": "x-trace=1,x-secret-key%3D{key}",
    "an encoded name": "x%2Dapi%2Dkey={key}",
    "doubly encoded delimiter": "a=b%252Cx-api-key%253D{key}",
}


@pytest.mark.parametrize("variable", ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"])
@pytest.mark.parametrize("form", list(ENCODED_FORMS))
@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_a_key_header_written_with_an_encoded_delimiter_is_refused_like_the_plain_form(variable, form, transport, listeners):
    if transport == "grpc":
        pytest.importorskip("grpc")
    listeners.reset()
    env = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    env.update({variable: ENCODED_FORMS[form].format(key=oc.MARKER_KEY.replace("{", "%7B").replace("}", "%7D")),
                "FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET})
    target = f"127.0.0.1:{listeners.target.port}"
    with oc.environment(env), pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        if transport == "http":
            otel.HTTPSpanExporter(endpoint=f"http://{target}/v1/traces", timeout=0.4)
        else:
            otel.GRPCSpanExporter(endpoint=target, insecure=True, timeout=0.4)
    assert raised.value.code == "key_header_in_environment"
    assert oc.MARKER_KEY not in str(raised.value) and "%3D" not in str(raised.value)
    assert listeners.measure() == {"dialed": None, "marker_bytes": 0}


@pytest.mark.parametrize("value", ["x-trace=1", "authorization-note=x-api-key-is-not-here", "a=b%2Cc%3Dd", "x-api-keys=1"])
def test_headers_that_do_not_name_a_key_header_are_not_refused(value, listeners):
    env = {name: None for name in oc.PROXY_NAMES + oc.OWN_NAMES}
    env.update({"OTEL_EXPORTER_OTLP_HEADERS": value, "FI_API_KEY": oc.MARKER_KEY, "FI_SECRET_KEY": oc.MARKER_SECRET})
    with oc.environment(env):
        otel.HTTPSpanExporter(endpoint=f"http://127.0.0.1:{listeners.target.port}/v1/traces", timeout=0.4).shutdown()


# ---- the 1.5.2 changelog labels every default-deny item as a break ---------------------------------------------------------

def _bullets():
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
    start = text.index("## [1.5.2]")
    section = text[start : text.index("\n## [", start + 1)]
    return [line for line in section.splitlines() if line.startswith("- ")]


@pytest.mark.parametrize("code", ["insecure_self_hosted_target", "insecure_futureagi_http", "insecure_futureagi_grpc", "empty_authority",
                                  "disputed_authority", "unverified_endpoint", "proxied", "key_header_in_environment"])
def test_every_default_deny_code_has_a_bullet_that_is_labelled_breaking_with_the_user_action(code):
    labelled = [b for b in _bullets() if f"`{code}`" in b and "Breaking:" in b and b.split("Breaking:", 1)[1].strip()]
    assert labelled, code
    # the sentence after the label states the code or the action: it is not empty and names what to change
    assert any(code in b.split("Breaking:", 1)[1] for b in labelled) or code in ("insecure_futureagi_grpc", "proxied", "key_header_in_environment")


def test_the_redirect_refusal_is_labelled_breaking():
    bullets = [b for b in _bullets() if "follows no redirect" in b]
    assert len(bullets) == 1 and "Breaking:" in bullets[0]
