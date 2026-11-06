"""Cloud-only TTY regression; only synthetic keys are entered into the child."""
import errno
import os
import pty
import select
import signal
import sys
import time
import termios

node, cli, mode = sys.argv[1:]
valid = mode in ("valid", "paste", "fresh")
api, secret = ("replacement-api", "replacement-secret") if valid else ("rejected-api", "rejected-secret")
pid, fd = pty.fork()
if pid == 0:
    os.execv(node, [node, cli, "setup", "pi", "--scope", "project", "--project", "coding-agents", "--configure-only"])
transcript = ""
steps = [("Replace them? [y/N]", "y"), ("API key (hidden):", api), ("Secret key (hidden):", secret)]
if mode == "fresh":
    steps = [("API key (hidden):", api), ("Secret key (hidden):", secret)]
elif mode == "paste":
    steps = [("Replace them? [y/N]", "y"), ("API key (hidden):", api + "\r\n" + secret)]
elif mode in ("cancel", "terminate"):
    steps = [("Replace them? [y/N]", "y"), ("API key (hidden):", "\x03")]
expected_steps = len(steps)
index = 0
status = None
try:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if select.select([fd], [], [], 0.1)[0]:
            try:
                data = os.read(fd, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not data:
                break
            transcript += data.decode(errors="replace")
            if index < len(steps) and steps[index][0] in transcript:
                if mode == "terminate" and index == 1:
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.write(fd, (steps[index][1] + "\r").encode())
                index += 1
        exited, code = os.waitpid(pid, os.WNOHANG)
        if exited:
            status = code
            break
    else:
        raise AssertionError("TTY recovery timed out")
    if status is None:
        _, status = os.waitpid(pid, 0)
    assert index == expected_steps, "Masked recovery prompts were not offered"
    assert termios.tcgetattr(fd)[3] & termios.ECHO, "Terminal echo was not restored"
    assert api not in transcript and secret not in transcript, "Credential input was echoed"
    assert os.waitstatus_to_exitcode(status) == (0 if valid else 2), "Unexpected recovery exit code"
    expected = "CONFIGURED_NOT_REGISTERED" if valid else ("CANCELLED" if mode in ("cancel", "terminate") else "AUTH_REJECTED")
    assert expected in transcript
    print("MASKED_RECOVERY_VALID" if valid else "MASKED_RECOVERY_REJECTED_ATOMICALLY")
finally:
    if status is None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    os.close(fd)
