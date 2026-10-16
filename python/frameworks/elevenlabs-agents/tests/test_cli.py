import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import FI_API_KEY, FI_SECRET_KEY, WEBHOOK_SECRET
from traceai_elevenlabs_agents.cli import _parser, main
from traceai_elevenlabs_agents.config import ConfigError, ForwarderConfig

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_missing_env_exits_2(capsys, monkeypatch):
    for name in ("FI_API_KEY", "FI_SECRET_KEY", "FI_PROJECT_NAME", "ELEVENLABS_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "FI_API_KEY" in err and "ELEVENLABS_WEBHOOK_SECRET" in err


def test_config_from_env_defaults_and_flags(tmp_path):
    env = {
        "FI_API_KEY": "apikey-value-1234",
        "FI_SECRET_KEY": "secretkey-value-5678",
        "FI_PROJECT_NAME": "p",
        "ELEVENLABS_WEBHOOK_SECRET": "webhook-secret-9012",
        "FI_ELEVENLABS_CAPTURE_CONTENT": "true",
        "FI_ELEVENLABS_STATE_PATH": str(tmp_path / "x.sqlite3"),
    }
    config = ForwarderConfig.from_env(env)
    assert config.capture_content is True
    assert config.fi_base_url == "https://api.futureagi.com"
    assert config.signature_tolerance_secs == 1800
    assert config.state_path.endswith("x.sqlite3")
    for value in ("apikey-value-1234", "secretkey-value-5678", "webhook-secret-9012"):
        assert value not in repr(config)
    assert ForwarderConfig.from_env({**env, "FI_ELEVENLABS_CAPTURE_CONTENT": ""}).capture_content is False
    with pytest.raises(ConfigError):
        ForwarderConfig.from_env({**env, "FI_ELEVENLABS_SIGNATURE_TOLERANCE_SECS": "abc"})
    # Backfill does not need the webhook secret.
    no_secret = {k: v for k, v in env.items() if k != "ELEVENLABS_WEBHOOK_SECRET"}
    assert ForwarderConfig.from_env(no_secret, require_webhook_secret=False).webhook_secret == ""


def test_serve_flags_after_subcommand_win():
    args = _parser().parse_args(["serve", "--port", "9100"])
    assert (args.command, args.port, args.host) == ("serve", 9100, "127.0.0.1")
    args = _parser().parse_args(["--port", "9200", "serve"])
    assert args.port == 9200


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_console_entry_point_serves_and_stops_on_sigterm(tmp_path):
    port = _free_port()
    env = dict(
        os.environ,
        FI_API_KEY=FI_API_KEY,
        FI_SECRET_KEY=FI_SECRET_KEY,
        FI_PROJECT_NAME="p",
        FI_BASE_URL="http://127.0.0.1:9",
        ELEVENLABS_WEBHOOK_SECRET=WEBHOOK_SECRET,
        FI_ELEVENLABS_STATE_PATH=str(tmp_path / "s.sqlite3"),
        PYTHONPATH=str(PACKAGE_ROOT),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "traceai_elevenlabs_agents", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=1) as r:
                    assert json.loads(r.read()) == {"status": "ok"}
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("forwarder did not start")
        proc.terminate()
        output = proc.communicate(timeout=10)[0].decode()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert "listening on" in output
    for secret in (FI_API_KEY, FI_SECRET_KEY, WEBHOOK_SECRET):
        assert secret not in output
