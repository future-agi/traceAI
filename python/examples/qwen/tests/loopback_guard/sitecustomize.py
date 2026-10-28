"""Install before application imports and block non-loopback DNS/connections."""

import os
import socket
from pathlib import Path

_getaddrinfo = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _check(host, operation):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host not in ("127.0.0.1", "localhost"):
        log = os.environ.get("LOOPBACK_GUARD_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(f"{operation}: {host}\n")
        raise RuntimeError(f"Loopback guard refused {operation} to {host}")


def _guarded_getaddrinfo(host, *args, **kwargs):
    _check(host, "getaddrinfo")
    return _getaddrinfo(host, *args, **kwargs)


def _guarded_connect(self, address):
    _check(address[0] if isinstance(address, tuple) else address, "connect")
    return _connect(self, address)


def _guarded_connect_ex(self, address):
    _check(address[0] if isinstance(address, tuple) else address, "connect_ex")
    return _connect_ex(self, address)


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex

if log := os.environ.get("LOOPBACK_GUARD_LOG"):
    Path(log).touch()
if ready := os.environ.get("LOOPBACK_GUARD_READY"):
    Path(ready).write_text("installed\n", encoding="utf-8")
