"""Installed at Python startup in tests to reject external connections before DNS."""

import os
import socket
from pathlib import Path

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _check(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host not in ("127.0.0.1", "localhost"):
        log = os.environ.get("LOOPBACK_GUARD_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as output:
                output.write("blocked non-loopback connection\n")
        raise OSError("loopback guard refused a non-loopback host before DNS")


def _guard_connect(self, address):
    _check(address[0] if isinstance(address, tuple) else address)
    return _connect(self, address)


def _guard_connect_ex(self, address):
    _check(address[0] if isinstance(address, tuple) else address)
    return _connect_ex(self, address)


def _guard_getaddrinfo(host, *args, **kwargs):
    _check(host)
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
socket.getaddrinfo = _guard_getaddrinfo
if ready := os.environ.get("LOOPBACK_GUARD_READY"):
    Path(ready).write_text("loopback guard installed\n", encoding="utf-8")
