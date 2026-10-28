#!/usr/bin/env bash
# All contracts and descendants share an otherwise empty user/network namespace.
# The first argv is the installed consumer Python used for mandatory probes;
# remaining argv is the actual test command. No host-network fallback exists.
set -euo pipefail
if [[ "${1:-}" != --inside ]]; then
  [[ $(uname -s) == Linux ]] || { echo 'Linux namespaces required' >&2; exit 90; }
  # GitHub ubuntu-24.04 restricts unprivileged user namespaces via AppArmor.
  # sudo only creates the new namespaces; no capabilities in the host namespace
  # survive exec. no-new-privs prevents an installed test binary gaining them.
  exec sudo --non-interactive env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    unshare --user --map-users="0:$(id -u):1" --map-groups="0:$(id -g):1" \
    --setuid=0 --setgid=0 --keep-caps --net -- \
    setpriv --no-new-privs bash "$(realpath "$0")" --inside "$@"
fi
shift
python=$1
shift
ip link set dev lo up
export FI_BASE_URL=http://127.0.0.1:9 FI_GRPC_URL=http://127.0.0.1:9
export HOME="${PWD}" PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
# pytest-asyncio is explicitly loaded, so unrelated dependency plugins cannot
# discover credentials or change collection/skips behind our back.
"$python" -I - <<'PY'
import json, os, socket, subprocess, sys
links = json.loads(subprocess.check_output(['ip', '-j', 'link', 'show']))
assert [link['ifname'] for link in links] == ['lo'], 'Unexpected external network interface'
assert len(open('/proc/net/route').read().splitlines()) == 1, 'External IPv4 route exists'
code = '''import socket
for family, host in ((socket.AF_INET, '1.1.1.1'), (socket.AF_INET6, '2606:4700:4700::1111')):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((host, 443))
    except OSError:
        pass
    else:
        raise AssertionError('External connection succeeded')
    finally:
        s.close()
'''
exec(code)
subprocess.run([sys.executable, '-I', '-c', code], check=True, env={'PATH': '/usr/bin:/bin'})
server = socket.socket()
server.bind(('127.0.0.1', 0))
server.listen(1)
child = subprocess.Popen([sys.executable, '-I', '-c',
    "import socket,sys; s=socket.create_connection(('127.0.0.1',int(sys.argv[1]))); s.sendall(b'loopback-ok'); s.close()",
    str(server.getsockname()[1])], env={'PATH': '/usr/bin:/bin'})
server.settimeout(5)
connection, _ = server.accept()
assert connection.recv(64) == b'loopback-ok'
connection.close()
server.close()
assert child.wait(timeout=5) == 0
print(json.dumps({'network_isolation': 'passed', 'interfaces': ['lo'], 'ipv4': 'denied',
                  'ipv6': 'denied', 'child_external': 'denied', 'child_loopback': 'passed'}), flush=True)
PY
exec "$@" -p pytest_asyncio.plugin
