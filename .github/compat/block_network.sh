#!/usr/bin/env bash
# No populated-interpreter invocation is allowed outside this boundary.
# A disposable host UID, an empty filesystem root, fresh PID/IPC/net namespaces,
# read-only consumer/toolchain mounts, and one writable evidence mount. No sockets
# or host /home, /root, /run, /tmp, /proc or workspace are exposed.
set -euo pipefail
if [[ "${1:-}" != --inside ]]; then
  [[ $(uname -s) == Linux ]] || { echo 'Linux sandbox required' >&2; exit 90; }
  root=$(realpath "$1"); output=$(realpath "$2"); toolchain=$(realpath "$3"); cwd=$(realpath "$4")
  shift 4
  [[ "$cwd" == "$root/"* && "$output" != "$root" ]] || exit 91
  # A filesystem socket bind is a network escape even with a separate netns.
  [[ -z $(find "$root" "$toolchain" -type s -print -quit) ]] || exit 92
  chmod 755 "$root"
  chmod 777 "$output"
  # Only synthetic account metadata is exposed to the ID-map helpers. No host
  # passwd/home/subordinate-ID database is shared with dependency execution.
  printf 'root:x:0:0:sandbox:/root:/bin/sh\nnobody:x:65534:65534:sandbox:/nonexistent:/usr/sbin/nologin\n' > "$root/passwd"
  printf 'root:x:0:\nnogroup:x:65534:\n' > "$root/group"
  printf 'root:65534:1\n' > "$root/subuid"
  cp "$root/subuid" "$root/subgid"
  # Set up mounts while privileged, BEFORE any dependency code starts. unshare
  # below maps namespace root to host nobody, NOT host root. Each job is ephemeral.
  args=(--die-with-parent --new-session --unshare-pid --unshare-net --unshare-ipc --unshare-uts
    --cap-add CAP_NET_ADMIN --cap-add CAP_SYS_ADMIN --cap-add CAP_SETUID --cap-add CAP_SETGID --cap-add CAP_SETPCAP
    --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /sbin /sbin --ro-bind /lib /lib
    --proc /proc --dev /dev --perms 1777 --tmpfs /tmp --tmpfs /run --perms 1777 --tmpfs /home
    --dir /etc --ro-bind "$root/passwd" /etc/passwd --ro-bind "$root/group" /etc/group
    --ro-bind "$root/subuid" /etc/subuid --ro-bind "$root/subgid" /etc/subgid
    --ro-bind "$root" "$root" --bind "$output" "$output"
    --chdir "$cwd" --clearenv
    --setenv PATH /usr/sbin:/usr/bin:/sbin:/bin)
  # bwrap creates implicit mount parents mode 0700. Make the interpreter's
  # synthetic parent directories traversable by the mapped disposable UID.
  parents=(); parent=$(dirname "$toolchain")
  while [[ "$parent" != / ]]; do
    parents=(--perms 0755 --dir "$parent" "${parents[@]}")
    parent=$(dirname "$parent")
  done
  args+=("${parents[@]}" --ro-bind "$toolchain" "$toolchain")
  [[ ! -e /lib64 ]] || args+=(--ro-bind /lib64 /lib64)
  [[ ! -d /etc/ssl/certs ]] || args+=(--ro-bind /etc/ssl/certs /etc/ssl/certs)
  exec sudo --non-interactive env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    bwrap "${args[@]}" /bin/bash "$root/block_network.sh" --inside "$root" "$output" "$toolchain" "$@"
fi
shift
root=$1; output=$2; toolchain=$3; shift 3
ip link set dev lo up
# UID/GID 0 inside is explicitly host UID/GID 65534 outside. No host-root
# mapping survives. Drop capabilities and no-new-privs before dependency startup.
exec unshare --user --map-users=65534,0,1 --map-groups=65534,0,1 --setuid=0 --setgid=0 -- \
  setpriv --no-new-privs --bounding-set=-all --inh-caps=-all --ambient-caps=-all \
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/home TMPDIR=/tmp \
  FI_BASE_URL=http://127.0.0.1:9 FI_GRPC_URL=http://127.0.0.1:9 \
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /bin/bash -c '
    set -euo pipefail
    root=$1; output=$2; toolchain=$3; shift 3
    namei -l "$toolchain/bin/python3" >&2
    # -S: mandatory probes use only the clean toolchain, never dependency .pth.
    "$toolchain/bin/python3" -I -S - "$root" "$output" <<'\''PY'\''
import json, os, pathlib, socket, subprocess, sys
root, output = map(pathlib.Path, sys.argv[1:])
for kind in ("uid", "gid"):
    mapping = pathlib.Path("/proc/self/" + kind + "_map").read_text().split()
    assert mapping == ["0", "65534", "1"], (kind, mapping)
assert os.getuid() == 0 and os.getgid() == 0
status = pathlib.Path("/proc/self/status").read_text()
assert "CapEff:\t0000000000000000" in status
assert "NoNewPrivs:\t1" in status
assert not pathlib.Path("/root").exists()
assert not any(pathlib.Path("/run").iterdir())
assert not any(pathlib.Path("/home").iterdir())
# The mount is read-only independently of DAC. Root in this user namespace
# cannot write the runner-owned consumer, interpreter or helper.
try:
    (root / "escape-write").write_text("bad")
except OSError:
    pass
else:
    raise AssertionError("Consumer mount is writable")
probe = output / "sandbox-write-probe"
probe.write_text("output writable")
probe.unlink()
# Host pathname sockets cannot be reached through a shared /run or /tmp.
s = socket.socket(socket.AF_UNIX)
try:
    s.connect("/run/docker.sock")
except OSError:
    pass
else:
    raise AssertionError("Host socket exposed")
finally:
    s.close()
links = json.loads(subprocess.check_output(["ip", "-j", "link", "show"]))
assert [link["ifname"] for link in links] == ["lo"]
assert len(pathlib.Path("/proc/net/route").read_text().splitlines()) == 1
code = """import socket
for family, host in ((socket.AF_INET, "1.1.1.1"), (socket.AF_INET6, "2606:4700:4700::1111")):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((host, 443))
    except OSError:
        pass
    else:
        raise AssertionError("External connection succeeded")
    finally:
        s.close()
"""
exec(code)
subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True)
server = socket.socket()
server.bind(("127.0.0.1", 0)); server.listen(1); server.settimeout(5)
child = subprocess.Popen([sys.executable, "-I", "-S", "-c",
    "import socket,sys; s=socket.create_connection((\"127.0.0.1\",int(sys.argv[1]))); s.sendall(b\"loopback-ok\"); s.close()",
    str(server.getsockname()[1])])
connection, _ = server.accept()
assert connection.recv(64) == b"loopback-ok"
connection.close(); server.close()
assert child.wait(timeout=5) == 0
print(json.dumps(dict(network_isolation="passed", host_uid=65534, host_gid=65534,
    filesystem="isolated-read-only", host_sockets="absent", ipv4="denied", ipv6="denied",
    child_external="denied", child_loopback="passed")), file=sys.stderr, flush=True)
PY
    exec "$@"
  ' sandbox "$root" "$output" "$toolchain" "$@"
