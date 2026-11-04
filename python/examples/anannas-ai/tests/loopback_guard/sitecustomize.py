"""Refuse non-loopback DNS and socket connections before they happen."""

import os
import socket
from pathlib import Path

_original_getaddrinfo = socket.getaddrinfo
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


def _check(host, operation):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host not in ("127.0.0.1", "localhost"):
        if log := os.environ.get("LOOPBACK_GUARD_LOG"):
            with open(log, "a", encoding="utf-8") as output:
                output.write(f"blocked {operation}\n")
        raise OSError(f"loopback guard refused {operation}")


def _getaddrinfo(host, *args, **kwargs):
    _check(host, "getaddrinfo")
    return _original_getaddrinfo(host, *args, **kwargs)


def _connect(self, address):
    _check(address[0] if isinstance(address, tuple) else address, "connect")
    return _original_connect(self, address)


def _connect_ex(self, address):
    _check(address[0] if isinstance(address, tuple) else address, "connect_ex")
    return _original_connect_ex(self, address)


socket.getaddrinfo = _getaddrinfo
socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex

if log := os.environ.get("LOOPBACK_GUARD_LOG"):
    with open(log, "a", encoding="utf-8"):
        pass
if ready := os.environ.get("LOOPBACK_GUARD_READY"):
    Path(ready).write_text("loopback guard installed\n", encoding="utf-8")
