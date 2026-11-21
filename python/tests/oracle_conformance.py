"""Drive the rows of the shared plain-channel oracle through this package's own exporters and request functions.

The oracle (``fixtures/plaintext-rule-oracle.json.gz``) states, for every combination of transport, host, source of the
plain channel, opt-in, proxy environment, bypass list and caller, the decision (build or refuse), the error code, the route
that is dialed and whether Future AGI keys may reach a sink. This module executes a row against the code and measures:

* a refused row must raise the error code the row names, and no byte of the marker keys may reach any sink;
* a built row must reach the route the row names (the target listener, or a proxy sink);
* every transport here sends its own marker keys, to loopback listeners with byte counters: one listener standing for
  every target, and two proxy sinks that understand CONNECT.

Applicability. ``http`` rows run through ``HTTPSpanExporter`` (``requests``), ``eval-config`` rows through
``check_custom_eval_config_exists`` and ``grpc`` rows through ``GRPCSpanExporter``. The exporter accepts a ``requests``
session from the caller, so the ``client_with_proxy`` and ``uninspectable_transport`` rows run for ``http`` only: the other two
transports take no client from the caller, so those rows have no code to run.
"""
import contextlib
import gzip
import hashlib
import json
import os
import pathlib
import socket
import threading
import time
import unittest.mock as mock
from urllib.parse import urlsplit

import requests

from fi_instrumentation import otel as _otel

HERE = pathlib.Path(__file__).resolve().parent
ORACLE_GZ = HERE / "fixtures" / "plaintext-rule-oracle.json.gz"
ORACLE_GZ_SHA256 = "bb75798b5c34c68e428ca9b31a17c8df33bfdede43844dd36ff728f70552d1eb"
ORACLE_JSON_SHA256 = "c2072ae9e74dc80543e10fabaf54da26554bf12d3033c7bbe46067cf8aac4f1e"

# The suite's autouse fixture replaces this function with a mock; the original is taken at import time.
CHECK_EXISTS = _otel.check_custom_eval_config_exists
# The suite's autouse fixture also replaces ``requests.post`` with a mock; the original is taken at import time too.
REAL_POST = requests.post

# The marker keys carry characters that HPACK codes with more than eight bits, so a header value that holds them is
# sent as literal bytes and a byte counter at the sink finds them.
MARKER_KEY = "mkr-key-7c1e~~^^``{{}}~~^^``{{}}"
MARKER_SECRET = "mkr-secret-7c1e~~^^``{{}}~~^^``{{}}"
MARKERS = (MARKER_KEY.encode(), MARKER_SECRET.encode())

PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy",
               "grpc_proxy", "GRPC_PROXY", "no_grpc_proxy", "NO_GRPC_PROXY")
OWN_NAMES = ("FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_GRPC_URL", "FI_TRACES_URL", "FI_ALLOW_INSECURE_HTTP",
             "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_INSECURE",
             "OTEL_EXPORTER_OTLP_TRACES_INSECURE", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")


def load_oracle():
    raw = ORACLE_GZ.read_bytes()
    return raw, json.loads(gzip.decompress(raw))


def digests():
    raw = ORACLE_GZ.read_bytes()
    return hashlib.sha256(raw).hexdigest(), hashlib.sha256(gzip.decompress(raw)).hexdigest()


# ---- listeners ---------------------------------------------------------------------------------------------------------

class Sink:
    """A loopback listener that counts connections and marker bytes. It answers a request it can read (200, or 403 to a
    CONNECT) and closes anything else (a TLS hello, an HTTP/2 preface) once the client has gone quiet."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self.reset()
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def reset(self):
        with self.lock:
            self.connections = 0
            self.inflight = 0
            self.marker_bytes = 0
            self.first_lines = []

    def close(self):
        self._stop = True
        with contextlib.suppress(OSError):
            self.sock.close()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
                self.inflight += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _complete(buf):
        head, sep, rest = buf.partition(b"\r\n\r\n")
        if not sep or not head.split(b" ", 1)[0].isalpha() or head.startswith(b"PRI "):
            return False
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip() or 0)
        return len(rest) >= length

    def _handle(self, conn):
        buf = b""
        h2 = False
        try:
            conn.settimeout(1.0)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if self._complete(buf):
                    break
                if buf.startswith(b"PRI * HTTP/2.0") and not h2:
                    # An HTTP/2 client sends its request headers after the server's SETTINGS frame.
                    h2 = True
                    conn.sendall(b"\x00\x00\x00\x04\x00\x00\x00\x00\x00")
                conn.settimeout(0.06 if h2 else 0.03)
        except (OSError, ValueError):
            pass
        try:
            first = buf.split(b"\r\n", 1)[0][:200]
            if first.startswith(b"CONNECT "):
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            elif self._complete(buf):
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()
            with self.lock:
                self.marker_bytes += sum(buf.count(marker) * len(marker) for marker in MARKERS)
                self.first_lines.append(buf.split(b"\r\n", 1)[0][:80])
                self.inflight -= 1


class Listeners:
    """The target listener T and the proxy sinks A and B, and a shim that sends every connection except the ones to A and B
    to T, so a name or an address that is not loopback reaches the target listener."""

    def __init__(self):
        self.target, self.a, self.b = Sink(), Sink(), Sink()

    def close(self):
        for sink in (self.target, self.a, self.b):
            sink.close()

    def reset(self):
        for sink in (self.target, self.a, self.b):
            sink.reset()

    def settle(self):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if all(sink.inflight == 0 for sink in (self.target, self.a, self.b)):
                return
            time.sleep(0.002)

    def measure(self):
        self.settle()
        t, a, b = self.target.connections, self.a.connections, self.b.connections
        route = "proxy" if (a or b) and not t else "target" if t and not (a or b) else None if not (t or a or b) else "mixed"
        return {"dialed": route, "marker_bytes": self.target.marker_bytes + self.a.marker_bytes + self.b.marker_bytes}

    @contextlib.contextmanager
    def shim(self):
        import urllib3.util.connection as connection

        real = connection.create_connection
        proxies = {self.a.port, self.b.port}
        target = self.target.port

        def create_connection(address, *args, **kwargs):
            host, port = address
            if host in ("127.0.0.1", "localhost") and port in proxies:
                return real(("127.0.0.1", port), *args, **kwargs)
            return real(("127.0.0.1", target), *args, **kwargs)

        with mock.patch.object(connection, "create_connection", create_connection):
            yield


# ---- rows --------------------------------------------------------------------------------------------------------------

NO_PROXY_VALUES = {
    "exact": lambda host: host,
    "bare": lambda host: host.split(".", 1)[1] if "." in host and host.split(".", 1)[1] else host,
    "dot_suffix": lambda host: "." + host.split(".", 1)[1] if "." in host and host.split(".", 1)[1] else "." + host,
    "star": lambda host: "*",
    "host_port": lambda host, port="{T}": f"{host}:{port}",
    "cidr": lambda host: "10.0.0.0/8" if host.startswith("10.") else "127.0.0.0/8",
}


def env_for(oracle, row, ports):
    """The proxy environment a row states, with the sink ports filled in."""
    dims = oracle["dimensions"]
    env = {name: None for name in PROXY_NAMES}
    for name, value in dims["env_values"][row["env"]].items():
        env[name] = value.replace("{A}", str(ports["A"])).replace("{B}", str(ports["B"]))
    variant = row["no_proxy"]
    if variant == "empty_both":
        env["NO_PROXY"] = env["no_proxy"] = ""
    elif variant != "none":
        authority = dims["hosts"][row["host"]]["authority"]
        host = authority.rsplit(":{T}", 1)[0]
        bare = host.strip("[]")
        value = NO_PROXY_VALUES[variant](bare)
        value = value.replace("{T}", str(ports["T"]))
        env["NO_PROXY"] = env["no_proxy"] = value
    return env


def endpoint_for(oracle, row, ports, plain):
    host = oracle["dimensions"]["hosts"][row["host"]]
    authority = host["authority"].replace("{T}", str(ports["T"]))
    path = host["path"] if host["path"] != "/" else ""
    return authority, path


@contextlib.contextmanager
def environment(updates):
    saved = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class OpaqueAdapter(requests.adapters.BaseAdapter):
    """A session adapter this package cannot look into: it dials the first proxy sink, as a caller's own transport might."""

    port = None

    def __init__(self, port=None, **settings):
        # A release that copies the caller's session builds the adapter again from its pool settings.
        super().__init__()
        self.port = port if port is not None else type(self).port

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1) as conn:
                conn.sendall(f"CONNECT {parts.hostname}:{parts.port} HTTP/1.1\r\n\r\n".encode())
                conn.recv(100)
        except OSError:
            pass
        raise requests.exceptions.ConnectionError("opaque transport")

    def close(self):
        return None


def caller_session(caller, ports):
    if caller == "none":
        return None
    session = requests.Session()
    if caller == "client_with_proxy":
        session.proxies = {"http": f"http://127.0.0.1:{ports['A']}", "https": f"http://127.0.0.1:{ports['A']}"}
    else:
        opaque = type("Opaque", (OpaqueAdapter,), {"port": ports["A"]})()
        session.mount("http://", opaque)
        session.mount("https://", opaque)
    return session


_SPAN = []


def one_span():
    if not _SPAN:
        from opentelemetry.sdk.trace import TracerProvider

        span = TracerProvider().get_tracer("oracle").start_span("row")
        span.end()
        _SPAN.append(span)
    return _SPAN[0]


class Recorder:
    """Records the gRPC channels an exporter opens and opens the real channel to the target listener."""

    def __init__(self, listeners):
        self.listeners = listeners
        self.channels = []

    @contextlib.contextmanager
    def patched(self):
        import grpc
        import opentelemetry.exporter.otlp.proto.grpc.exporter as base

        real_secure, real_insecure = grpc.secure_channel, grpc.insecure_channel
        target = f"127.0.0.1:{self.listeners.target.port}"

        def secure(addr, credentials, *args, **kwargs):
            self.channels.append(("tls", addr, kwargs.get("options") or (args[0] if args else None), isinstance(credentials, grpc.ChannelCredentials)))
            return real_secure(target, credentials, *args, **kwargs)

        def insecure(addr, *args, **kwargs):
            self.channels.append(("plain", addr, kwargs.get("options") or (args[0] if args else None), False))
            return real_insecure(target, *args, **kwargs)

        with contextlib.ExitStack() as stack:
            for owner in (grpc, base):
                if hasattr(owner, "secure_channel"):
                    stack.enter_context(mock.patch.object(owner, "secure_channel", secure))
                if hasattr(owner, "insecure_channel"):
                    stack.enter_context(mock.patch.object(owner, "insecure_channel", insecure))
            yield


def send_grpc(exporter):
    """One export call on the exporter's own channel and metadata, without the exporter's retry loop."""
    import grpc

    try:
        exporter._client.Export(request=exporter._translate_data([one_span()]), metadata=exporter._headers, timeout=0.4)
    except grpc.RpcError:
        pass


PORTS_SEEN = {}


def run_row(oracle, row, listeners, opt_in, caller, unguarded=False):
    """Execute one row. Returns ``{"decision", "error", "dialed", "marker_bytes", "channel"}``."""
    dims = oracle["dimensions"]
    transport, source = row["transport"], row["source"]
    ports = {"A": listeners.a.port, "B": listeners.b.port, "T": listeners.target.port}
    PORTS_SEEN.update(ports)
    authority, path = endpoint_for(oracle, row, ports, source != "tls")
    env = {name: None for name in PROXY_NAMES + OWN_NAMES}
    env.update(env_for(oracle, row, ports))
    env.update({"FI_API_KEY": MARKER_KEY, "FI_SECRET_KEY": MARKER_SECRET, "FI_ALLOW_INSECURE_HTTP": opt_in})
    plain_scheme = "https" if source == "tls" else "http"
    kwargs = {}
    if transport == "grpc":
        if source == "tls":
            env["FI_GRPC_URL"] = f"https://{authority}{path}"
        elif source == "http_scheme":
            env["FI_GRPC_URL"] = f"http://{authority}{path}"
        else:
            env["FI_GRPC_URL"] = f"{authority}{path}"
            if source == "insecure_argument":
                kwargs["insecure"] = True
            else:
                env["OTEL_EXPORTER_OTLP_INSECURE"] = "true"
    else:
        env["FI_BASE_URL"] = f"{plain_scheme}://{authority}{path}"
    outcome = {"decision": "build", "error": None, "dialed": None, "marker_bytes": 0, "channel": None}
    listeners.reset()
    otel = _otel
    otel._PLAIN_TEXT_WARNED.clear()
    session = caller_session(caller, ports) if transport == "http" else None
    recorder = Recorder(listeners)
    try:
        with environment(env), listeners.shim(), contextlib.ExitStack() as stack:
            if transport == "grpc":
                stack.enter_context(recorder.patched())
            if unguarded:
                stack.enter_context(mock.patch.object(otel, "_authorize_plain_text", lambda *a, **k: None))
            try:
                if transport == "http":
                    exporter = otel.HTTPSpanExporter(timeout=0.4, **({"session": session} if session is not None else {}))
                    try:
                        # OpenTelemetry 1.29 lets a failed request raise out of export(); the processors catch it.
                        with contextlib.suppress(requests.exceptions.RequestException):
                            exporter.export([one_span()])
                    finally:
                        exporter.shutdown()
                elif transport == "grpc":
                    exporter = otel.GRPCSpanExporter(timeout=1, **kwargs)
                    try:
                        send_grpc(exporter)
                    finally:
                        exporter.shutdown()
                else:
                    with mock.patch.object(requests, "post", REAL_POST):
                        CHECK_EXISTS("project", ["tag"], base_url=env["FI_BASE_URL"])
            except otel.UnsupportedFutureAGIEndpointError as error:
                outcome["decision"] = "refuse"
                outcome["error"] = getattr(error, "code", None)
    finally:
        if session is not None:
            session.close()
    measured = listeners.measure()
    outcome.update(measured)
    if outcome["decision"] == "refuse":
        outcome["dialed"] = None
    if recorder.channels:
        outcome["channel"] = recorder.channels[-1]
    return outcome


# ---- the rows to run and the comparison --------------------------------------------------------------------------------

CALLERS = ("none", "client_with_proxy", "uninspectable_transport")
# Rows of the oracle that a ruling decides, listed by host so the list is the only place a difference lives. The ruling is
# ORACLE-S1-AT-IN-PATH-RULED-EXCEPTION-r1-FISEC.json (fi-security): a URL with an ``@`` after its authority is refused as
# ``disputed_authority`` on every row of the host, whatever the oracle row says, and no key reaches any sink. These rows are
# counted as ``ruled_exception``; every other row must match the oracle (https rows included).
RULING = "ORACLE-S1-AT-IN-PATH-RULED-EXCEPTION-r1-FISEC.json"
RULED_EXCEPTION = {"s1_at_in_path": "disputed_authority"}
# The NO_PROXY value of a row with a bypass list is not stated by the oracle; ``env_for`` reconstructs it from the host of the
# row (see ``NO_PROXY_VALUES``). Such rows are counted as ``derived``, apart from the rows whose environment the oracle states.
CATEGORIES = ("matched", "ruled_exception", "derived")


def classify(row):
    """The category of a row: ``ruled_exception`` (decided by the ruling), ``derived`` (its NO_PROXY value is reconstructed) or
    ``matched`` (its environment is stated by the oracle and the answer must equal the row's)."""
    if row["host"] in RULED_EXCEPTION:
        return "ruled_exception"
    return "matched" if row["no_proxy"] == "none" else "derived"


def cases(oracle, transport):
    """(row, opt_in, caller) for every row that applies to ``transport``, and the number of rows that do not apply."""
    optins = oracle["dimensions"]["opt_in"]
    out, skipped = [], 0
    for row in oracle["rows"]:
        if row["transport"] != transport:
            continue
        if transport == "http":
            callers = CALLERS if row["caller"] == "*" else (row["caller"],)
        elif row["caller"] in ("none", "*"):
            callers = ("none",)
        else:
            skipped += 1
            continue
        if row["opt_in"] != "*":
            values = (optins[row["opt_in"]],)
        else:
            values = (None, "1") if row["decision"] == "refuse" else (None,)
        for caller in callers:
            for value in values:
                out.append((row, value, caller))
    return out, skipped


def expected(row, transport, caller):
    """The decision, error and route this package must answer for a row, or None for a route that is not compared."""
    if row["decision"] == "refuse":
        return ("refuse", row["error"], None)
    if row["source"] != "tls" and caller == "none":
        return ("build", None, row["dialed"])
    if transport == "grpc":
        return ("build", None, "unchecked")
    if caller != "none":
        return ("build", None, "proxy")
    # A TLS request is routed by the library that sends it: requests, here. The row's route is the union over every library.
    return ("build", None, "proxy" if "requests" in row["proxied_by"] else "target")


def problems(oracle, row, transport, caller, outcome):
    """What differs between a measured outcome and the row, as strings; empty when it conforms."""
    found = []
    want = expected(row, transport, caller)
    got = (outcome["decision"], outcome["error"], outcome["dialed"])
    group = oracle["dimensions"]["hosts"][row["host"]]["group"]
    ruled = RULED_EXCEPTION.get(row["host"])
    if ruled:
        if got[:2] != ("refuse", ruled) or outcome["marker_bytes"]:
            return [f"the ruling {RULING} says refuse {ruled} with no marker byte, answered {got} with {outcome['marker_bytes']} bytes"]
        return []
    if want[2] == "unchecked":
        want, got = want[:2], got[:2]
    if want != got:
        found.append(f"answered {got}, the row says {want}")
    if outcome["decision"] == "refuse" or row["source"] == "tls":
        if outcome["marker_bytes"]:
            found.append(f"{outcome['marker_bytes']} marker bytes at a sink")
    elif outcome["decision"] == "build" and not outcome["marker_bytes"]:
        found.append("a built plain channel delivered no key")
    if transport == "grpc" and row["source"] == "tls" and outcome["decision"] == "build":
        channel = outcome["channel"]
        if not channel:
            found.append("a built TLS row opened no channel")
        else:
            kind, address, raw_options, has_credentials = channel[0], channel[1], channel[2], channel[3]
            options = dict(o for o in (raw_options or ()) if isinstance(o, tuple))
            configured = oracle["dimensions"]["hosts"][row["host"]]["authority"].replace("{T}", str(PORTS_SEEN.get("T", "")))
            if kind != "tls" or not has_credentials:
                found.append(f"the channel is {kind!r} (credentials: {has_credentials}), not TLS with credentials")
            if address != configured:
                found.append(f"the channel targets {address!r}, the configured host is {configured!r}")
            if any(name in options for name in ("grpc.enable_http_proxy", "grpc.http_proxy")):
                found.append("a TLS channel was given a proxy option")
    if row["source"] != "tls" and transport == "grpc" and outcome["decision"] == "build":
        options = dict(o for o in (outcome["channel"][2] or ()) if isinstance(o, tuple)) if outcome["channel"] else {}
        if options.get("grpc.enable_http_proxy") != 0 or outcome["channel"][0] != "plain":
            found.append("a plain channel was built without the proxy turned off")
    del group
    return found
