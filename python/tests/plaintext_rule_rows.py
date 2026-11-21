"""Rows for the plain-channel rule, and the runner that executes one row against the code.

``tests/fixtures/plaintext_rule_rows.json`` holds one row per cell. Each row names a transport (``grpc``, ``http`` or
``eval-config``), the endpoint, the source of a plain channel, the opt-in, the proxy environment and the expected
outcome: the decision, the error name, the host:port that is dialed, whether the channel is TLS or plain, and
whether Future AGI keys are on it. ``python -m plaintext_rule_rows`` (run from ``tests``) rewrites the file and its
SHA256SUMS from the code and refuses to write a row that breaks the invariants below.
"""
import contextlib
import hashlib
import json
import os
import pathlib
import sys
from urllib.parse import urlsplit

from fi_instrumentation import otel as _otel

# The suite's autouse fixture replaces this function with a mock; the original is taken at import time.
_CHECK_EXISTS = _otel.check_custom_eval_config_exists
HERE = pathlib.Path(__file__).resolve().parent
ROWS_FILE = HERE / "fixtures" / "plaintext_rule_rows.json"
SUMS_FILE = HERE / "fixtures" / "SHA256SUMS"
KEY = "synthetic-env-key"
SECRET = "synthetic-env-secret"
ERROR = "UnsupportedFutureAGIEndpointError"

PROXIES = {
    "none": {},
    "HTTP_PROXY": {"HTTP_PROXY": "http://proxy.internal:3128"},
    "http_proxy": {"http_proxy": "http://proxy.internal:3128"},
    "HTTPS_PROXY-alone": {"HTTPS_PROXY": "http://proxy.internal:3128"},
    "ALL_PROXY": {"ALL_PROXY": "http://proxy.internal:3128"},
    "NO_PROXY-exact": {"HTTP_PROXY": "http://proxy.internal:3128", "NO_PROXY": "collector.internal"},
    "NO_PROXY-suffix": {"HTTP_PROXY": "http://proxy.internal:3128", "NO_PROXY": ".internal"},
    "NO_PROXY-port": {"HTTP_PROXY": "http://proxy.internal:3128", "NO_PROXY": "collector.internal:8080,collector.internal:50051"},
    "NO_PROXY-star": {"HTTP_PROXY": "http://proxy.internal:3128", "NO_PROXY": "*"},
    "NO_PROXY-cidr": {"HTTP_PROXY": "http://proxy.internal:3128", "NO_PROXY": "10.0.0.0/8"},
    "caller-proxies": {},
}
# gRPC channels are routed by what grpcio reads (measured on grpcio 1.63.2, 1.70.0 and 1.85.3): the lower-case names grpc_proxy,
# https_proxy and http_proxy (the first one set decides), the bypass list no_grpc_proxy (when set) or no_proxy; never ALL_PROXY or
# an upper-case name; a bypass entry with a port or "*" does not bypass; a network range bypasses an address, not a name.
GRPC_PROXIES = {
    "none": {},
    "grpc_proxy": {"grpc_proxy": "http://proxy.internal:3128"},
    "https_proxy-lower": {"https_proxy": "http://proxy.internal:3128"},
    "http_proxy-lower": {"http_proxy": "http://proxy.internal:3128"},
    "HTTPS_PROXY-upper-ignored": {"HTTPS_PROXY": "http://proxy.internal:3128"},
    "HTTP_PROXY-upper-ignored": {"HTTP_PROXY": "http://proxy.internal:3128"},
    "ALL_PROXY-ignored": {"ALL_PROXY": "http://proxy.internal:3128"},
    "https_proxy-empty-shadows-http_proxy": {"https_proxy": "", "http_proxy": "http://proxy.internal:3128"},
    "no_proxy-exact": {"grpc_proxy": "http://proxy.internal:3128", "no_proxy": "collector.internal"},
    "no_proxy-suffix": {"grpc_proxy": "http://proxy.internal:3128", "no_proxy": ".internal"},
    "no_proxy-port-does-not-bypass": {"grpc_proxy": "http://proxy.internal:3128", "no_proxy": "collector.internal:50051"},
    "no_proxy-star-does-not-bypass": {"grpc_proxy": "http://proxy.internal:3128", "no_proxy": "*"},
    "no_proxy-cidr": {"grpc_proxy": "http://proxy.internal:3128", "no_proxy": "10.0.0.0/8"},
    "NO_PROXY-upper-ignored": {"grpc_proxy": "http://proxy.internal:3128", "NO_PROXY": "collector.internal"},
    "no_grpc_proxy-exact": {"grpc_proxy": "http://proxy.internal:3128", "no_grpc_proxy": "collector.internal"},
    "no_grpc_proxy-over-no_proxy": {"grpc_proxy": "http://proxy.internal:3128", "no_grpc_proxy": "other.example", "no_proxy": "collector.internal"},
    "no_grpc_proxy-empty-shadows-no_proxy": {"grpc_proxy": "http://proxy.internal:3128", "no_grpc_proxy": "", "no_proxy": "collector.internal"},
}
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy",
                   "grpc_proxy", "GRPC_PROXY", "no_grpc_proxy", "NO_GRPC_PROXY")
OPT_INS = {"unset": None, "1": "1", "true": "true"}
GRPC_SOURCES = {
    "scheme-or-none": ({}, {}),
    "insecure-argument": ({"insecure": True}, {}),
    "insecure-env": ({}, {"OTEL_EXPORTER_OTLP_INSECURE": "true"}),
    "traces-insecure-env": ({}, {"OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true"}),
}

HOSTS = {
    "http": {
        "futureagi": ["https://api.futureagi.com"],
        "futureagi-trailing-dot": ["https://api.futureagi.com.", "http://api.futureagi.com./"],
        "futureagi-plain": ["http://api.futureagi.com"],
        "self-hosted": ["http://collector.internal:8080", "https://collector.internal"],
        "literal-loopback": ["http://localhost:4318", "http://127.0.0.1:4318", "http://[::1]:4318"],
        "written-not-loopback": ["http://127.0.0.1.:4318", "http://127.1:4318", "http://0x7f.0.0.1:4318", "http://2130706433:4318",
                                 "http://localhost.:4318", "http://app.localhost:4318"],
        "look-alike": ["https://api.futureagi.com:8443", "https://myfutureagi.corp", "https://futureagi.com.example", "https://ａｐｉ.futureagi.com"],
        "disputed-authority": ["http://localhost\\@evil.example:4318", "https://api.futureagi.com\\@evil.example:4318",
                               "http://localhost%5C@evil.example:4318", "http://localhost:4318/path@evil.example", "http://user@evil.example:4318"],
        "empty-authority": ["https:///api.futureagi.com/", "http:///api.futureagi.com"],
    },
    "grpc": {
        "futureagi": ["grpc.futureagi.com:443", "https://grpc.futureagi.com"],
        "futureagi-trailing-dot": ["grpc.futureagi.com.:443", "http://grpc.futureagi.com."],
        "futureagi-plain-scheme": ["http://grpc.futureagi.com"],
        "self-hosted": ["collector.internal:50051", "http://collector.internal:50051", "https://collector.internal:50051"],
        "literal-loopback": ["localhost:50051", "127.0.0.1:50051", "[::1]:50051", "http://127.0.0.1:50051"],
        "written-not-loopback": ["127.0.0.1.:50051", "http://127.1:50051", "0x7f.0.0.1:50051", "2130706433:50051", "localhost.:50051",
                                 "app.localhost:50051"],
        "look-alike": ["grpc.futureagi.com:4317", "myfutureagi.corp:4317", "futureagi.com.example:4317", "ｇｒｐｃ.futureagi.com:443"],
        "disputed-authority": ["localhost\\@evil.example:4317", "grpc://grpc.futureagi.com\\@evil.example:4317",
                               "grpc://localhost%5C@evil.example:4317", "user@evil.example:4317", "grpc.futureagi.com:443\n",
                               "https://grpc.futureagi.com:443\n", "grpc.futureagi.com:443 "],
    },
}
HOSTS["eval-config"] = HOSTS["http"]


ENV_GROUPS = ("futureagi", "futureagi-trailing-dot", "futureagi-plain", "futureagi-plain-scheme", "self-hosted", "literal-loopback", "written-not-loopback")
PLAIN_PROXY_GROUPS = ("self-hosted", "written-not-loopback", "literal-loopback", "futureagi-plain", "futureagi-plain-scheme")


def row_specs():
    """Every row to generate: a spec without its expected outcome.

    Hosts of the first groups are named by ``FI_BASE_URL`` or ``FI_GRPC_URL``; look-alike, disputed and empty-authority forms
    are given both that way and as the exporter's endpoint argument.
    """
    specs = []
    for transport, groups in HOSTS.items():
        for group, endpoints in groups.items():
            for endpoint in endpoints:
                vias = ["env"] if group in ENV_GROUPS else ["env", "argument"]
                if transport == "eval-config":
                    vias = ["env", "argument"] if group not in ENV_GROUPS else ["env"]
                plain = endpoint.startswith("http://") or transport == "grpc"
                for via in vias:
                    sources = GRPC_SOURCES if transport == "grpc" and group in ENV_GROUPS else {"scheme-or-none": ({}, {})}
                    for source in sources:
                        for opt_name in (OPT_INS if plain else ("unset",)):
                            specs.append(_spec(transport, group, endpoint, source, opt_name, "none", via))
                if group in PLAIN_PROXY_GROUPS and plain:
                    for proxy in (GRPC_PROXIES if transport == "grpc" else PROXIES):
                        if proxy == "none" or (proxy == "caller-proxies" and transport != "http"):
                            continue
                        specs.append(_spec(transport, group, endpoint, "scheme-or-none", "1", proxy, "env"))
    seen, unique = set(), []
    for spec in specs:
        if spec["id"] not in seen:
            seen.add(spec["id"])
            unique.append(spec)
    return unique


def _spec(transport, group, endpoint, source, opt_name, proxy, via):
    return {
        "id": f"{transport}|{endpoint!r}|via={via}|{source}|opt-in={opt_name}|proxy={proxy}",
        "transport": transport, "host_group": group, "endpoint": endpoint, "via": via, "source": source,
        "opt_in": OPT_INS[opt_name], "proxy": proxy,
    }


@contextlib.contextmanager
def _environment(updates):
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


def _dialed(target):
    """``host:port`` of an endpoint as a client dials it (after the exporter's own rewrite), or None."""
    text = target
    if "://" in text:
        text = urlsplit(text).netloc or text
    return text or None


def run_row(spec):
    """Execute one spec against the code, with every channel and request replaced by a recorder. Returns the outcome."""
    import requests

    otel = _otel

    transport = spec["transport"]
    env = {name: None for name in PROXY_VARIABLES}
    env.update({"FI_API_KEY": KEY, "FI_SECRET_KEY": SECRET, "FI_BASE_URL": "http://127.0.0.1:9", "FI_GRPC_URL": "127.0.0.1:9",
                "FI_TRACES_URL": None, "FI_ALLOW_INSECURE_HTTP": spec["opt_in"], "OTEL_EXPORTER_OTLP_HEADERS": None,
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS": None, "OTEL_EXPORTER_OTLP_INSECURE": None, "OTEL_EXPORTER_OTLP_TRACES_INSECURE": None})
    env.update(GRPC_PROXIES[spec["proxy"]] if transport == "grpc" else PROXIES[spec["proxy"]])
    transport, endpoint = spec["transport"], spec["endpoint"]
    kwargs = {}
    if transport == "grpc":
        kwargs, source_env = GRPC_SOURCES[spec["source"]]
        env.update(source_env)
        if spec["via"] == "env":
            env["FI_GRPC_URL"] = endpoint
        else:
            kwargs = {**kwargs, "endpoint": endpoint}
    elif spec["via"] == "env":
        env["FI_BASE_URL"] = endpoint
    else:
        kwargs = {"endpoint": endpoint + ("/tracer/v1/traces" if "://" in endpoint else "")} if transport == "http" else {}
    outcome = {"decision": None, "error": None, "dialed": None, "tls": None, "keys": False}
    channels, posts = [], []
    patched = []

    def patch(owner, name, value):
        if hasattr(owner, name):
            patched.append((owner, name, getattr(owner, name)))
            setattr(owner, name, value)

    if transport == "grpc":
        import grpc
        import opentelemetry.exporter.otlp.proto.grpc.exporter as grpc_base

        real_secure, real_insecure = grpc.secure_channel, grpc.insecure_channel
        for owner in (grpc, grpc_base):
            patch(owner, "secure_channel", lambda target, *a, **k: (channels.append(("tls", target)), real_secure("127.0.0.1:9", *a, **k))[1])
            patch(owner, "insecure_channel", lambda target, *a, **k: (channels.append(("plain", target)), real_insecure("127.0.0.1:9", *a, **k))[1])

    class Reply:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"exists": False}}

    patch(requests, "post", lambda url, headers=None, **k: (posts.append((url, headers or {})), Reply())[1])
    patch(requests.Session, "post", lambda self, url, headers=None, **k: (posts.append((url, headers or {})), Reply())[1])
    otel._PLAIN_TEXT_WARNED.clear()
    try:
        with _environment(env):
            outcome["decision"] = otel._future_agi_decision(endpoint, grpc=transport == "grpc", named=spec["via"] == "env")
            session = None
            if spec["proxy"] == "caller-proxies":
                session = requests.Session()
                session.proxies = {"https": "http://proxy.internal:3128"}
            try:
                if transport == "grpc":
                    exporter = otel.GRPCSpanExporter(timeout=1, **kwargs)
                    outcome["keys"] = any(str(k).lower() == "x-api-key" for k, _ in (exporter._headers or ()))
                    exporter.shutdown()
                elif transport == "http":
                    exporter = otel.HTTPSpanExporter(timeout=1, **kwargs, **({"session": session} if session is not None else {}))
                    names = {str(k).lower() for k in getattr(getattr(exporter, "_client", None), "_headers", {}) or {}}
                    names |= {str(k).lower() for k in getattr(getattr(exporter, "_session", None), "headers", {}) or {}}
                    held = getattr(exporter, "_headers", None) or {}
                    names |= {str(k).lower() for k in (held if isinstance(held, dict) else [pair[0] for pair in held])}
                    outcome["keys"] = "x-api-key" in names
                    outcome["dialed"] = _dialed(exporter._endpoint)
                    outcome["tls"] = "tls" if urlsplit(exporter._endpoint).scheme == "https" else "plain"
                    exporter.shutdown()
                else:
                    _CHECK_EXISTS("project", ["tag"], base_url=endpoint if spec["via"] == "argument" else None)
                    url, headers = posts[0]
                    outcome["keys"] = any(str(k).lower() == "x-api-key" for k in headers)
                    outcome["dialed"] = _dialed(url)
                    outcome["tls"] = "tls" if urlsplit(url).scheme == "https" else "plain"
            except otel.UnsupportedFutureAGIEndpointError as error:
                outcome["error"] = type(error).__name__
                outcome["keys"] = False
                outcome["dialed"], outcome["tls"] = None, None
            if transport == "grpc" and outcome["error"] is None:
                kinds = sorted({kind for kind, _ in channels})
                outcome["tls"] = kinds[0] if len(kinds) == 1 else None
                targets = sorted({target for _, target in channels})
                outcome["dialed"] = targets[0] if len(targets) == 1 else None
    finally:
        for owner, name, value in reversed(patched):
            setattr(owner, name, value)
    return outcome


def check_invariants(spec, outcome):
    """The oracle: facts that hold for every row whatever the code does. Returns the broken ones."""
    problems = []
    group, opt_in = spec["host_group"], spec["opt_in"]
    if outcome["keys"] and outcome["error"]:
        problems.append("keys on a refused row")
    if group in ("futureagi", "futureagi-trailing-dot", "futureagi-plain", "futureagi-plain-scheme") and outcome["keys"] and outcome["tls"] != "tls":
        problems.append("keys to a futureagi.com host without TLS")
    if group in ("look-alike", "disputed-authority", "empty-authority") and outcome["keys"] and spec["via"] == "argument":
        problems.append("keys to a form that is not verified")
    if group in ("disputed-authority", "empty-authority") and outcome["keys"]:
        problems.append("keys to a disputed or empty authority")
    if outcome["keys"] and outcome["tls"] == "plain":
        loopback = group == "literal-loopback"
        if not loopback and not (opt_in == "1" and group in ("self-hosted", "written-not-loopback")):
            problems.append("keys on a plain channel without a literal loopback host or the opt-in")
        if not loopback and spec["proxy"] == "caller-proxies":
            problems.append("keys on a plain channel with a session of the caller's that has a proxy")
    if opt_in in (None, "true") and group in ("self-hosted", "written-not-loopback") and outcome["tls"] == "plain" and outcome["keys"]:
        problems.append("keys on a plain channel without the exact opt-in")
    return problems


def generate():
    rows, broken = [], []
    for spec in row_specs():
        outcome = run_row(spec)
        for problem in check_invariants(spec, outcome):
            broken.append((spec["id"], problem))
        rows.append({**spec, "expect": outcome})
    return rows, broken


def serialise(rows):
    return json.dumps({"rule": "FI keys go to Future AGI hosts only over TLS; a plain channel needs literal loopback or FI_ALLOW_INSECURE_HTTP=1",
                       "rows": rows}, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def main():
    rows, broken = generate()
    if broken:
        for item in broken:
            print("INVARIANT", *item, file=sys.stderr)
        raise SystemExit(1)
    text = serialise(rows)
    ROWS_FILE.write_text(text, encoding="utf-8")
    SUMS_FILE.write_text(f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}  plaintext_rule_rows.json\n", encoding="utf-8")
    print(len(rows), "rows")


if __name__ == "__main__":
    main()
