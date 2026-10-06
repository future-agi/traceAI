"""Refuse non-loopback connections before DNS in recipe subprocesses."""

import os
import socket
from pathlib import Path

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _check(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host not in ("127.0.0.1", "localhost"):
        log = os.environ.get("LOOPBACK_GUARD_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as file:
                file.write(f"REFUSED {host}\n")
        raise OSError("Loopback guard refused a non-loopback host")


def _guarded_connect(self, address):
    _check(address[0] if isinstance(address, tuple) else address)
    return _connect(self, address)


def _guarded_connect_ex(self, address):
    _check(address[0] if isinstance(address, tuple) else address)
    return _connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    _check(host)
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo
if ready := os.environ.get("LOOPBACK_GUARD_READY"):
    Path(ready).write_text("LOOPBACK_GUARD_READY\n", encoding="utf-8")
