"""Process-start S5 regressions with real SDKs and a loopback OTLP collector."""

import gzip
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from .conftest import V2

APP = Path(__file__).with_name("_zero_code_app.py")
HEAVY = Path("/Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh")


def run_app(command, env, cwd):
    # The containing pytest process also uses the shared-host budget. Each child
    # runtime must independently enter the wrapper, never bypass host safety.
    runtime_env = env
    if HEAVY.is_file():
        # The budget wrapper needs the real HOME for host-status.sh; apply the
        # isolated application environment only after the wrapper has its slot.
        command = [
            "bash", str(HEAVY), "env", "-i",
            *(f"{key}={value}" for key, value in env.items()), *command,
        ]
        runtime_env = os.environ.copy()
    with subprocess.Popen(
        command, env=runtime_env, cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
    assert process.returncode == 0, stdout + stderr
    (result,) = [
        line.removeprefix("TRACEAI_RESULT ")
        for line in stdout.splitlines()
        if line.startswith("TRACEAI_RESULT ")
    ]
    return json.loads(result), stdout + stderr


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "source",
    [
        "userdict",
        pytest.param(
            "dynamic",
            marks=pytest.mark.skipif(
                not V2, reason="installed OpenAI 1.x has no async API-key provider"
            ),
        ),
    ],
)
def test_zero_code_agent_redacts_transmitted_credential(endpoint, source, stream):
    # The credential is an argument, never an environment value: only the final
    # instrumented transport can learn it. Every case gets fresh child processes.
    secret = f"zero-code-{source}-{'stream' if stream else 'plain'}-20261030"
    endpoint.echo_header = "Authorization"
    payloads = []

    class Collector(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
            payloads.append(payload)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="traceai-agentscope-s5-", dir="/tmp") as scratch:
            config_file = Path(scratch) / "futureagi.toml"
            config_file.write_text("", encoding="utf-8")
            # Do not inherit real credentials, provider endpoints, proxy settings,
            # hide flags, config files, or another agent's startup hooks.
            env = {
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                "HOME": scratch,
                "TMPDIR": scratch,
                "PYTHONDONTWRITEBYTECODE": "1",
                "FI_API_KEY": "synthetic-fi-api-20261030",
                "FI_SECRET_KEY": "synthetic-fi-secret-20261030",
                "FI_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "FI_PROJECT_NAME": "agentscope-s5-loopback",
                "FI_INSTRUMENTATIONS": "wire",
                "FI_CONFIG_FILE": str(config_file),
                "OTEL_TRACES_EXPORTER": "none",
                "OTEL_METRICS_EXPORTER": "none",
                "OTEL_LOGS_EXPORTER": "none",
            }
            args = [
                sys.executable, str(APP), "--endpoint", endpoint.url,
                "--credential", secret, "--source", source,
            ] + (["--stream"] if stream else [])
            baseline, _ = run_app(args, env, scratch)
            assert not payloads
            active, diagnostics = run_app(
                [str(Path(sys.executable).with_name("opentelemetry-instrument")), *args],
                env, scratch,
            )
        assert active == baseline
        assert active["outputs"][-1] == "echoed: " + secret
        assert active["provider_calls"] == (1 if source == "dynamic" else 0)
        assert len(endpoint.headers) == 2
        assert endpoint.headers[0] == endpoint.headers[1]
        assert endpoint.headers[1]["authorization"] == "Bearer " + secret
        assert payloads, diagnostics
        spans = []
        for payload in payloads:
            assert secret.encode() not in payload
            batch = MessageToDict(ExportTraceServiceRequest.FromString(payload))
            assert secret not in json.dumps(batch)
            for resource in batch["resourceSpans"]:
                for scope in resource["scopeSpans"]:
                    spans.extend(scope["spans"])
        # S4 suppressed the wire span but did not suppress credential observation.
        assert len(spans) == 2
        (llm,) = [span for span in spans if span["name"].startswith("agentscope.")]
        attrs = {item["key"]: item["value"] for item in llm["attributes"]}
        assert attrs["gen_ai.span.kind"] == {"stringValue": "LLM"}
        assert attrs["traceai.capture.layer"] == {"stringValue": "library"}
        assert attrs["output.value"] == {"stringValue": "echoed: <redacted>"}
        (later,) = [span for span in spans if span["name"] == "later-echo"]
        assert later["traceId"] != llm["traceId"]
        assert not later.get("parentSpanId")
        assert {item["key"]: item["value"] for item in later["attributes"]}[
            "output.value"
        ] == {"stringValue": "later: <redacted>"}
        assert later["events"][0]["attributes"][0]["value"] == {
            "stringValue": "<redacted>"
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
