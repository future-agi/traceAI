"""Synthetic PTY test; never consults a real credential store or agent home."""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time

node, cli, work = sys.argv[1:]
root = Path(tempfile.mkdtemp(prefix="tty-", dir=work))
master, slave = pty.openpty()
before = termios.tcgetattr(slave)
process = subprocess.Popen(
    [node, cli, "auth", "add", "--profile", "fixture", "--project", "fixture", "--storage", "file", "--tty"],
    stdin=slave, stdout=slave, stderr=slave, cwd=root,
    env={"PATH": "/usr/bin:/bin", "HOME": str(root), "FI_AGENT_CONFIG_HOME": str(root / "config"), "FI_AGENT_STATE_HOME": str(root / "state")},
)
output = b""
stage = 0
try:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            output += os.read(master, 4096)
            if stage == 0 and b"API key (input hidden)" in output:
                os.write(master, b"synthetic-api-tty-canary\r")
                stage = 1
            if stage == 1 and b"Secret key (input hidden)" in output:
                os.write(master, b"synthetic-secret-tty-canary\r")
                stage = 2
        if process.poll() is not None:
            break
    if process.poll() is None:
        process.kill()
    code = process.wait()
    after = termios.tcgetattr(slave)
    result = {"exit": code, "masked": b"tty-canary" not in output, "terminal_restored": before == after, "both_prompts": stage == 2}
    print(json.dumps(result))
    if code != 0 or not all([result["masked"], result["terminal_restored"], result["both_prompts"]]):
        sys.exit(1)
finally:
    os.close(master)
    os.close(slave)
