"""Cloud-only terminal regression: replacement keys are hidden and saved atomically."""
import errno
import http.server
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import threading
import time

CLI = Path(__file__).resolve().parents[1] / "dist/cli.js"
accepted = "old-synthetic-api"


class Validator(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200 if self.headers.get("X-Api-Key") == accepted else 401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_):
        pass


def replacement(env, home, key):
    master, slave = pty.openpty()
    proc = subprocess.Popen([
        "node", str(CLI), "setup", "copilot", "--project-dir", str(home),
        "--project", "coding-agents", "--configure-only", "--replace-credentials",
    ], env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    transcript = b""
    sent = set()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                transcript += chunk
            for prompt, value in [(b"API key (hidden): ", key),
                                  (b"Secret key (hidden): ", "replacement-synthetic-secret")]:
                if prompt in transcript and prompt not in sent:
                    os.write(master, value.encode() + b"\r")
                    sent.add(prompt)
        assert len(sent) == 2, "replacement did not prompt for both keys"
        code = proc.wait(timeout=5)
        assert key.encode() not in transcript, "API key was echoed"
        assert b"replacement-synthetic-secret" not in transcript, "secret key was echoed"
        return code
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Validator)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with tempfile.TemporaryDirectory(prefix="fi-credential-pty-") as tmp:
        home = Path(tmp)
        env = {"PATH": os.environ["PATH"], "HOME": tmp,
               "FI_AGENT_CONFIG_DIR": str(home / "config"),
               "XDG_STATE_HOME": str(home / "state"),
               "FI_OTLP_ENDPOINT": f"http://127.0.0.1:{server.server_port}/tracer/v1/traces"}
        initial = subprocess.run([
            "node", str(CLI), "setup", "copilot", "--project-dir", tmp,
            "--project", "coding-agents", "--configure-only", "--json",
        ], env={**env, "FI_API_KEY": accepted, "FI_SECRET_KEY": "old-synthetic-secret"},
            capture_output=True, check=True)
        saved = home / "config/credentials.json"
        before = saved.read_bytes()
        accepted = "new-synthetic-api"
        assert replacement(env, home, "rejected-synthetic-api") == 2
        assert saved.read_bytes() == before, "failed replacement changed saved profile"
        assert replacement(env, home, accepted) == 0
        store = json.loads(saved.read_text())
        assert store["profiles"][store["defaultProfile"]]["apiKey"] == accepted
        assert saved.stat().st_mode & 0o777 == 0o600
        print(json.dumps({"credential_replacement_pty": "PASS", "masked": True,
                          "failed_replacement_preserves_profile": True}))
finally:
    server.shutdown()
    server.server_close()
    thread.join()
