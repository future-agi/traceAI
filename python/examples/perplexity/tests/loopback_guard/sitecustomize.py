"""Refuse non-loopback DNS and socket connections before networking occurs."""

import os
import socket
from pathlib import Path

_ALLOWED = {"127.0.0.1", "localhost"}


def _check(host, operation):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if str(host).lower() not in _ALLOWED:
        log = os.environ.get("LOOPBACK_GUARD_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as stream:
                stream.write(f"{operation}: refused {host}\n")
        raise RuntimeError(f"Loopback guard refused {operation} to {host}")


_getaddrinfo = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _guarded_getaddrinfo(host, *args, **kwargs):
    _check(host, "getaddrinfo")
    return _getaddrinfo(host, *args, **kwargs)


def _guarded_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _check(address[0], "connect")
    return _connect(sock, address)


def _guarded_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _check(address[0], "connect_ex")
    return _connect_ex(sock, address)


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
if os.environ.get("LOOPBACK_GUARD_LOG"):
    Path(os.environ["LOOPBACK_GUARD_LOG"]).touch()
if os.environ.get("LOOPBACK_GUARD_READY"):
    Path(os.environ["LOOPBACK_GUARD_READY"]).write_text("installed\n", encoding="utf-8")
