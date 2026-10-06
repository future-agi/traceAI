"""Refuse non-loopback network access before DNS or socket connection."""

import os
import socket
from pathlib import Path

_getaddrinfo = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _check(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host not in ("127.0.0.1", "localhost"):
        log = os.environ.get("LOOPBACK_GUARD_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as output:
                output.write("REFUSED non-loopback host\n")
        raise RuntimeError("Loopback guard refused non-loopback network access")


def _guarded_getaddrinfo(host, *args, **kwargs):
    _check(host)
    return _getaddrinfo(host, *args, **kwargs)


def _guarded_connect(self, address):
    _check(address[0])
    return _connect(self, address)


def _guarded_connect_ex(self, address):
    _check(address[0])
    return _connect_ex(self, address)


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex

if ready := os.environ.get("LOOPBACK_GUARD_READY"):
    Path(ready).write_text("installed\n", encoding="utf-8")
