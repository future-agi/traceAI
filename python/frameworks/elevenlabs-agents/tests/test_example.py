"""The shipped E2E example runs green against a loopback collector (keeps examples/ honest)."""

import json
import os
import subprocess
import sys
from pathlib import Path

from test_server import Collector
from traceai_elevenlabs_agents.map import attributes_to_dict

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_run_e2e_example_against_loopback_collector(tmp_path):
    collector = Collector()
    try:
        env = dict(
            os.environ,
            FI_API_KEY="fi_example_key_0123456789",
            FI_SECRET_KEY="fi_example_secret_0123456789",
            FI_BASE_URL=collector.base_url,
            PYTHONPATH=str(PACKAGE_ROOT),
        )
        out = tmp_path / "report.json"
        done = subprocess.run(
            [sys.executable, str(PACKAGE_ROOT / "examples" / "run_e2e.py"), "--project", "example", "--out", str(out)],
            env=env,
            capture_output=True,
            timeout=240,
        )
        assert done.returncode == 0, done.stdout.decode()[-2000:] + done.stderr.decode()[-2000:]
        report = json.loads(out.read_text())
        assert report["ok"] is True
        assert [p["project"] for p in report["phases"]] == ["example", "example-optin"]
        forwarded = [s for p in report["phases"] for s in p["steps"] if s["response"].get("status") == "forwarded"]
        assert len(forwarded) == 6  # per phase: good, error, GET-only backfill
        # 6 exports reached the collector: nothing for duplicates, rejections or the dead collector.
        assert len(collector.requests) == 6
        bodies = [json.loads(r["body"]) for r in collector.requests]
        off_marker = report["phases"][0]["marker"]
        on_marker = report["phases"][1]["marker"]
        def project(body):
            return attributes_to_dict(body["resourceSpans"][0]["resource"]["attributes"])["project_name"]

        off = [b for b in bodies if project(b) == "example"]
        assert len(off) == 3 and off_marker not in json.dumps(off)
        assert on_marker in json.dumps(bodies)
    finally:
        collector.close()
