"""Installed-wheel .pth regression for repeated HTTPX credentials.

Run with the installed environment's Python. Both provider and collector are loopback;
credentials are synthetic and are created in the child, not seeded from its environment.
"""

import asyncio
import importlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread


VALUES = ["INSTALLED_DUPLICATE_A_12345678,comma-part", "INSTALLED_DUPLICATE_B_12345678"]


def child(url, module, asynchronous):
    from opentelemetry import trace

    httpx = importlib.import_module(module)
    tracer = trace.get_tracer("application")
    results = []

    def late_headers(request):
        request.headers.update([("X-API-Key", VALUES[0]), ("x-api-key", VALUES[1])])

    async def async_late_headers(request):
        late_headers(request)

    def run():
        if asynchronous:
            async def send():
                async with httpx.AsyncClient(event_hooks={"request": [async_late_headers]}) as client:
                    return await client.post(url, json={"model": "test", "messages": []})
            response = asyncio.run(send())
        else:
            with httpx.Client(event_hooks={"request": [late_headers]}) as client:
                response = client.post(url, json={"model": "test", "messages": []})
        return response.status_code, response.text, response.request.headers.get_list("x-api-key")

    for kind in ("CHAIN", "LLM"):
        with tracer.start_as_current_span("outer-" + kind, attributes={"gen_ai.span.kind": kind}) as outer:
            with tracer.start_as_current_span("anthropic.messages.create") as native:
                result = run()
                native.set_attribute("output.value", result[1])
            outer.set_attribute("output.value", result[1])
        results.append(result)

    def unrelated():
        with tracer.start_as_current_span("unrelated") as span:
            span.set_attribute("output.value", " ".join(VALUES))
            span.add_event("echo", {"exception.message": " ".join(VALUES)})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(unrelated).result(5)
    print(json.dumps(results))


@contextmanager
def provider_server():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"choices": [{"message": {
                "content": " ".join(self.headers.get_all("X-API-Key", []))}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def main():
    from auto_mock_server import MockServer, decode_spans

    import fi_instrumentation
    # The parent must be launched outside the source package, with a rebuilt wheel.
    assert Path(fi_instrumentation.__file__).is_relative_to(Path(sys.prefix))
    for module in ("httpx", "httpx2"):
        for asynchronous in (False, True):
            with provider_server() as url, MockServer() as collector:
                clean_env = {k: v for k, v in os.environ.items()
                             if not k.startswith(("FI_", "OTEL_", "PYTHON"))}
                clean_env.update(PYTHONDONTWRITEBYTECODE="1", OTEL_TRACES_EXPORTER="none",
                                 OTEL_METRICS_EXPORTER="none", OTEL_LOGS_EXPORTER="none")
                command = [sys.executable, "-B", str(Path(__file__).resolve()), "child", url,
                           module, str(int(asynchronous))]
                baseline = subprocess.run(command, env=clean_env, capture_output=True, text=True, timeout=30)
                assert baseline.returncode == 0, baseline.stderr
                assert not collector.collected
                env = dict(clean_env, FI_AUTO_INSTRUMENT="true", FI_API_KEY="SYNTHETIC_FI_KEY_12345678",
                           FI_SECRET_KEY="SYNTHETIC_FI_SECRET_12345678", FI_BASE_URL=collector.url,
                           FI_INSTRUMENTATIONS="wire")
                actual = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
                assert actual.returncode == baseline.returncode, actual.stderr
                assert json.loads(actual.stdout) == json.loads(baseline.stdout)
                assert all(result[2] == VALUES and result[0] == 200 for result in json.loads(actual.stdout))
                spans = decode_spans(collector.collected)
                assert len(spans) == 6, [span["name"] for span in spans]
                assert sum(s["attributes"].get("traceai.capture.layer") == "wire" for s in spans) == 1
                assert {s["name"] for s in spans} >= {"outer-CHAIN", "outer-LLM", "unrelated",
                                                       "anthropic.messages.create"}
                assert all(s["attributes"].get("output.value") == "<redacted> <redacted>"
                           for s in spans if s["name"] == "unrelated")
                # Check the complete serialized OTLP, not only the decoded attributes.
                assert all(value.encode() not in payload for value in VALUES for payload in collector.collected)
            print(f"{module} {'async' if asynchronous else 'sync'}: baseline equal; 6 spans; OTLP credentials scrubbed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child(sys.argv[2], sys.argv[3], bool(int(sys.argv[4])))
    else:
        main()
