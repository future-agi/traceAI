"""Refuse non-loopback DNS and socket connections before any network activity."""

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
            with open(log, "a", encoding="utf-8") as file:
                file.write("Refused non-loopback host\n")
        raise OSError("loopback guard refused non-loopback host")


def _guard_connect(self, address):
    _check(address[0])
    return _connect(self, address)


def _guard_connect_ex(self, address):
    _check(address[0])
    return _connect_ex(self, address)


def _guard_getaddrinfo(host, *args, **kwargs):
    _check(host)
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
socket.getaddrinfo = _guard_getaddrinfo
if os.environ.get("LOOPBACK_GUARD_LOG"):
    Path(os.environ["LOOPBACK_GUARD_LOG"]).touch()
if os.environ.get("LOOPBACK_GUARD_READY"):
    Path(os.environ["LOOPBACK_GUARD_READY"]).write_text("installed\n", encoding="utf-8")
