"""Drive the real CLI in a PTY with synthetic replacement keys, never real secrets."""
from pathlib import Path
import os
import pty
import select
import signal
import sys
import time

pid, fd = pty.fork()
if pid == 0:
    os.execvp("node", ["node", "--require", str(Path(__file__).with_name("prompt-delay.cjs")), sys.argv[1], "setup", "opencode", "--project", "coding-agents", "--configure-only"])
output = b""
sent_api = sent_secret = False
status = None
try:
    end = time.monotonic() + 15
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                status = os.waitpid(pid, 0)[1]
                break
            if not chunk:
                status = os.waitpid(pid, 0)[1]
                break
            output += chunk
            if b"API key (hidden): " in output and not sent_api:
                os.write(fd, b"synthetic-replacement-api\r")
                sent_api = True
            if b"Secret key (hidden): " in output and not sent_secret:
                os.write(fd, b"synthetic-replacement-secret\r")
                sent_secret = True
        done, result = os.waitpid(pid, os.WNOHANG)
        if done:
            status = result
            break
    if status is None:
        done, status = os.waitpid(pid, os.WNOHANG)
        if not done:
            raise AssertionError("CLI did not exit before deadline")
    assert sent_api and sent_secret, "replacement prompts missing"
    assert b"synthetic-replacement" not in output, "credentials echoed in PTY"
    assert os.waitstatus_to_exitcode(status) == int(sys.argv[2]), "unexpected setup exit"
    print("masked credential replacement verified")
finally:
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass
    os.close(fd)
