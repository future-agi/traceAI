"""Run the documented console command with synthetic credentials and loopback APIs."""

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from conftest import WEBHOOK_SECRET, raw
from test_rag import API_KEY, CONTENT, rag_api  # noqa: F401
from test_server import Collector, post
from traceai_elevenlabs_agents.verify import sign


def test_readme_console_command_enriches_a_signed_delivery(rag_api, tmp_path):
    package = Path(__file__).resolve().parents[1]
    readme = (package / "README.md").read_text()
    command = "traceai-elevenlabs-agents --host 0.0.0.0 --port 8787"
    assert command in readme
    assert "export FI_ELEVENLABS_ENRICH_RAG=true" in readme
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    collector = Collector()
    args = shlex.split(command)
    args[0] = str(Path(sys.executable).with_name("traceai-elevenlabs-agents"))
    args[-1] = str(port)  # use an available port, not a developer's existing receiver
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "ELEVENLABS_", "PYTHONPATH"))}
    env.update({
        "FI_API_KEY": "fi_synthetic_api", "FI_SECRET_KEY": "fi_synthetic_secret",
        "FI_PROJECT_NAME": "elevenlabs-calls", "ELEVENLABS_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "FI_ELEVENLABS_ENRICH_RAG": "true", "ELEVENLABS_API_KEY": API_KEY,
        "FI_ELEVENLABS_API_BASE_URL": rag_api.base_url, "FI_BASE_URL": collector.base_url,
        "FI_ELEVENLABS_STATE_PATH": str(tmp_path / "forwarded.sqlite3"),
    })
    process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    url = "http://127.0.0.1:%d" % port
    try:
        deadline = time.monotonic() + 15
        while True:
            assert process.poll() is None, "The documented receiver command exited before it became healthy"
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=1) as response:
                    assert response.status == 200
                break
            except (urllib.error.URLError, OSError):
                if time.monotonic() >= deadline:
                    raise AssertionError("The documented receiver command never became healthy")
                time.sleep(0.05)
        body = raw(rag_api.delivery)
        status, result = post(url + "/elevenlabs/webhook", body, {
            "Content-Type": "application/json", "ElevenLabs-Signature": sign(WEBHOOK_SECRET, body),
        })
        assert (status, result["status"]) == (200, "forwarded")
        assert len(collector.requests) == 1
        assert CONTENT in json.dumps(json.loads(collector.requests[0]["body"]))
        assert len(rag_api.requests) == 2
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=10)
        collector.close()
