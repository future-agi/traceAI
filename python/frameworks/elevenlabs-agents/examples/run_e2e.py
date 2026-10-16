"""End-to-end run of the real forwarder process against a real Future AGI collector.

ElevenLabs itself is a STANDIN here (see standin.py): deliveries are built from the
docs-shaped fixtures and signed locally with a throwaway secret.

What runs:
  1. ``traceai-elevenlabs-agents serve`` as a subprocess, configured only through the
     environment variables the docs page lists (content capture OFF), pointed at FI_BASE_URL.
  2. Deliveries: a good conversation, a duplicate (ElevenLabs retry), an abnormal-disconnect
     conversation (root ERROR, failing tool), bad / stale / missing / mutated signatures,
     other webhook types, a payload without resourceSpans.
  3. ``traceai-elevenlabs-agents backfill`` for the delivered conversation (must be a
     duplicate) and for a conversation that only exists on the GET stand-in.
  4. Collector failure: a second forwarder pointed at a closed port returns 502.
  5. SIGTERM, then the same run with FI_ELEVENLABS_CAPTURE_CONTENT=true into a second project.

Writes a JSON report (statuses, conversation ids, trace ids, markers) to --out.

    FI_API_KEY=... FI_SECRET_KEY=... FI_BASE_URL=http://localhost:14338 \\
      python examples/run_e2e.py --project my-elevenlabs --out e2e-report.json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import standin  # noqa: E402

PATH = "/elevenlabs/webhook"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cli() -> List[str]:
    exe = shutil.which("traceai-elevenlabs-agents")
    return [exe] if exe else [sys.executable, "-m", "traceai_elevenlabs_agents"]


def start_forwarder(env: Dict[str, str], port: int, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "ab")
    proc = subprocess.Popen(cli() + ["serve", "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(200):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=1):
                return proc
        except OSError:
            if proc.poll() is not None:
                raise SystemExit("forwarder exited early, see %s" % log_path) from None
            time.sleep(0.05)
    proc.kill()
    raise SystemExit("forwarder did not start")


def stop(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        return -9


def sdk_oracle():
    """The real ElevenLabs SDK, when installed: checks the stand-in signs and serves what it accepts."""
    try:
        from importlib.metadata import version

        from elevenlabs.client import ElevenLabs
    except ImportError:
        return None, None
    return ElevenLabs, version("elevenlabs")


def signed(body: Dict[str, Any], secret: str, phase: Dict[str, Any]):
    raw = json.dumps(body, separators=(",", ":")).encode()
    header = standin._sign(secret, raw, int(time.time()))
    client_cls, sdk_version = sdk_oracle()
    if client_cls is not None:
        event = client_cls(api_key="unused").webhooks.construct_event(rawBody=raw.decode(), sig_header=header, secret=secret)
        phase.setdefault("sdk_checks", []).append(
            {"check": "construct_event accepted stand-in delivery", "sdk": sdk_version, "type": event["type"]}
        )
    return raw, header


def run_phase(args, *, project: str, capture: bool, report: Dict[str, Any], out_dir: Path) -> None:
    phase: Dict[str, Any] = {"project": project, "capture_content": capture, "steps": []}
    marker = standin.unique_marker()
    phase["marker"] = marker
    secret = "whsec_standin_" + secrets.token_hex(16)
    xi_key = "xi_standin_" + secrets.token_hex(16)
    port = free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "ELEVENLABS_WEBHOOK_SECRET": secret,
        "FI_API_KEY": os.environ["FI_API_KEY"],
        "FI_SECRET_KEY": os.environ["FI_SECRET_KEY"],
        "FI_PROJECT_NAME": project,
        "FI_BASE_URL": os.environ.get("FI_BASE_URL", "https://api.futureagi.com"),
        "FI_ELEVENLABS_STATE_PATH": str(out_dir / ("state-%s.sqlite3" % ("on" if capture else "off"))),
    }
    if capture:
        env["FI_ELEVENLABS_CAPTURE_CONTENT"] = "true"
    log_path = out_dir / ("forwarder-%s.log" % ("on" if capture else "off"))
    log_path.write_bytes(b"")
    url = "http://127.0.0.1:%d%s" % (port, PATH)

    def step(name: str, expect: int, status: int, payload: Dict[str, Any], **extra: Any) -> None:
        ok = status == expect
        phase["steps"].append({"step": name, "expect": expect, "status": status, "ok": ok, "response": payload, **extra})
        print("%-4s %-44s HTTP %d %s" % ("ok" if ok else "FAIL", name, status, json.dumps(payload)[:120]))

    proc = start_forwarder(env, port, log_path)
    get_server = standin.GetStandin(xi_key)
    try:
        good = standin.build_delivery("post_call_transcription_otel.json", marker)
        raw_good, header = signed(good, secret, phase)
        status, payload = standin.send(url, raw_good, secret, header=header)
        step("conversation (tool call, 2 LLM turns)", 200, status, payload, conversation_id=good["data"]["conversation_id"])
        status, payload = standin.send(url, good, secret, timestamp=int(time.time()) + 1)
        step("same conversation again (retry)", 200, status, payload)

        bad = standin.build_delivery("post_call_transcription_otel_error.json", marker)
        raw_bad, header = signed(bad, secret, phase)
        status, payload = standin.send(url, raw_bad, secret, header=header)
        step("abnormal disconnect (root ERROR)", 200, status, payload, conversation_id=bad["data"]["conversation_id"])

        probe = standin.build_delivery("post_call_transcription_otel.json", marker)
        raw = json.dumps(probe, separators=(",", ":")).encode()
        step("wrong secret", 401, *standin.send(url, raw, "not-the-secret"))
        step("stale timestamp (31 min)", 401, *standin.send(url, raw, secret, timestamp=int(time.time()) - 31 * 60))
        step("missing signature header", 401, *standin.send(url, raw, secret, header=""))
        valid_header = standin._sign(secret, raw, int(time.time()))
        mutated = raw.replace(b"Goodbye", b"G00dbye")
        step("mutated body, valid header", 401, *standin.send(url, mutated, secret, header=valid_header))
        others = json.loads((standin.FIXTURES / "other_webhook_types.json").read_text().replace("{{MARKER}}", marker))
        for kind, body in others.items():
            step("type %s" % kind, 200, *standin.send(url, body, secret))
        empty = standin.build_delivery("post_call_transcription_otel.json", marker)
        empty["data"]["otlp_traces"] = {"resourceSpans": []}
        step("missing resourceSpans", 400, *standin.send(url, empty, secret))

        # Backfill: the delivered conversation converges; a GET-only conversation is forwarded.
        get_server.add_from_delivery(good)
        only_get = standin.build_delivery("post_call_transcription_otel.json", marker)
        get_server.add_from_delivery(only_get, user_id="user-" + marker)
        client_cls, sdk_version = sdk_oracle()
        if client_cls is not None:
            fetched = client_cls(api_key=xi_key, base_url=get_server.base_url).conversational_ai.conversations.get(
                conversation_id=only_get["data"]["conversation_id"], format="opentelemetry"
            )
            phase.setdefault("sdk_checks", []).append(
                {"check": "conversations.get(format=opentelemetry) parsed the GET stand-in", "sdk": sdk_version,
                 "spans": sum(len(ss["spans"]) for rs in fetched.otlp_traces["resourceSpans"] for ss in rs["scopeSpans"])}
            )
        backfill_env = dict(env, ELEVENLABS_API_KEY=xi_key, FI_ELEVENLABS_API_BASE_URL=get_server.base_url)
        backfill_env.pop("ELEVENLABS_WEBHOOK_SECRET")
        for name, conv, expect_status in (
            ("backfill of the delivered conversation", good["data"]["conversation_id"], "duplicate"),
            ("backfill of a GET-only conversation", only_get["data"]["conversation_id"], "forwarded"),
        ):
            done = subprocess.run(
                cli() + ["backfill", "--conversation-id", conv], env=backfill_env, capture_output=True, timeout=120
            )
            with open(log_path, "ab") as log:
                log.write(done.stderr)
            try:
                payload = json.loads(done.stdout.decode().strip().splitlines()[-1])
            except (IndexError, ValueError):
                payload = {"stdout": done.stdout.decode()[-200:]}
            status = 200 if done.returncode == 0 and payload.get("status") == expect_status else 500
            step(name, 200, status, payload, conversation_id=conv)

        # Collector failure: same delivery through a forwarder whose collector is down.
        dead_port, dead_env = free_port(), dict(env, FI_BASE_URL="http://127.0.0.1:%d" % free_port())
        dead_env["FI_ELEVENLABS_STATE_PATH"] = str(out_dir / "state-dead.sqlite3")
        dead_log = out_dir / "forwarder-dead.log"
        dead_log.write_bytes(b"")
        dead = start_forwarder(dead_env, dead_port, dead_log)
        try:
            lost = standin.build_delivery("post_call_transcription_otel.json", marker)
            step(
                "collector down -> 5xx so ElevenLabs can retry",
                502,
                *standin.send("http://127.0.0.1:%d%s" % (dead_port, PATH), lost, secret),
                conversation_id=lost["data"]["conversation_id"],
            )
        finally:
            stop(dead)
    finally:
        get_server.close()
        phase["exit_code"] = stop(proc)
    phase["ok"] = all(s["ok"] for s in phase["steps"]) and phase["exit_code"] == 0
    report["phases"].append(phase)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="content-off project")
    parser.add_argument("--optin-project", help="content-on project (default: <project>-optin)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    for name in ("FI_API_KEY", "FI_SECRET_KEY"):
        if not os.environ.get(name):
            raise SystemExit("%s is not set" % name)
    out = Path(args.out)
    out_dir = out.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {"started": int(time.time()), "standin": "ElevenLabs sender and GET endpoint", "phases": []}
    run_phase(args, project=args.project, capture=False, report=report, out_dir=out_dir)
    run_phase(args, project=args.optin_project or args.project + "-optin", capture=True, report=report, out_dir=out_dir)
    report["ok"] = all(p["ok"] for p in report["phases"])
    out.write_text(json.dumps(report, indent=2))
    print("report: %s  ok=%s" % (out, report["ok"]))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
