"""The platform API requests that carry Future AGI keys follow the same endpoint rules as the exporters."""
import socket
import threading
import types

import pytest
import requests

from fi_instrumentation import otel, settings

# The suite's autouse fixture replaces these two with mocks; the originals are taken at import time.
GET_TEMPLATE = settings.get_custom_eval_template
CHECK_EXISTS = otel.check_custom_eval_config_exists
REAL_POST = requests.post
FI_KEY, FI_SECRET = "synthetic-env-key", "synthetic-env-secret"
KEY_NAMES = {"x-api-key", "x-secret-key"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "FI_TRACES_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")


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
    monkeypatch.setattr(requests.Session, "post", lambda self, url, headers=None, **kw: post(url, headers=headers))
    return calls


def _template(base):
    return GET_TEMPLATE("some-eval", base_url=base)


def _exists(base):
    return CHECK_EXISTS("project", ["tag"], base_url=base)


def _collector(base):
    from fi_instrumentation.auto._cli import check_collector

    config = types.SimpleNamespace(has_credentials=True, base_url=base, api_key=FI_KEY, secret_key=FI_SECRET, export_off=None)
    return check_collector(config)


SITES = [_template, _exists, _collector]
SITE_IDS = ["eval-template", "eval-exists", "collector-check"]

REFUSED = [
    "http://api.futureagi.com",
    "https://api.futureagi.com:8443",
    "https://futureagi.com",
    "https://ａｐｉ.futureagi.com",
    "https://user:pw@api.futureagi.com",
    "https://evil.example%40api.futureagi.com",
]


@pytest.mark.parametrize("site", SITES, ids=SITE_IDS)
@pytest.mark.parametrize("base", REFUSED)
def test_a_futureagi_host_that_is_not_an_https_exact_origin_is_refused_before_any_request(site, base, sent):
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError) as raised:
        site(base)
    assert "FI_BASE_URL" in str(raised.value)
    assert FI_KEY not in str(raised.value) and FI_SECRET not in str(raised.value)
    assert sent == []


OWN = ["https://collector.example", "http://otel-collector:4318/futureagi", "https://myfutureagi.corp"]


@pytest.mark.parametrize("site", [_template, _exists], ids=["eval-template", "eval-exists"])
@pytest.mark.parametrize("base", OWN)
def test_an_own_collector_gets_the_request_without_keys(site, base, sent):
    site(base)
    assert len(sent) == 1 and KEY_NAMES.isdisjoint(sent[0][1])


@pytest.mark.parametrize("site", SITES, ids=SITE_IDS)
def test_the_default_futureagi_base_gets_the_keys_over_https(site, monkeypatch, sent):
    monkeypatch.setenv("FI_BASE_URL", "https://api.futureagi.com")
    site("https://api.futureagi.com")
    assert len(sent) == 1 and sent[0][0].startswith("https://api.futureagi.com")
    assert sent[0][1].get("x-api-key") == FI_KEY


class _Listener:
    """A raw TCP listener that records every byte it receives and answers a minimal HTTP reply."""

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
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
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
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: real("127.0.0.1" if host == "evil.example" else host, *a, **k))
    yield server
    server.close()


@pytest.mark.parametrize("site", [_template, _exists], ids=["eval-template", "eval-exists"])
def test_no_key_bytes_reach_a_listener_for_a_refused_form(site, listener):
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        site(f"http://evil.example:{listener.port}\\@api.futureagi.com")
    assert bytes(listener.received) == b""


@pytest.mark.parametrize("site", [_template, _exists], ids=["eval-template", "eval-exists"])
def test_no_key_bytes_reach_an_own_collector(site, listener):
    site(f"http://evil.example:{listener.port}")
    assert listener.received and FI_KEY.encode() not in bytes(listener.received)
    assert FI_SECRET.encode() not in bytes(listener.received)


@pytest.mark.parametrize("host", ["api.futureagi.com.", "FUTUREAGI.COM.", "grpc.futureagi.com"])
def test_one_trailing_dot_is_read_as_absent_for_a_futureagi_host(host):
    assert otel._is_futureagi_host(host)


@pytest.mark.parametrize("host", ["localhost.", "127.0.0.1.", "LOCALHOST"])
def test_one_trailing_dot_is_read_as_absent_for_a_loopback_host(host):
    assert otel._is_loopback_host(host)
