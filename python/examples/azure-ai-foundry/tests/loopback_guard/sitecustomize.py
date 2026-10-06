"""Refuse non-loopback networking before DNS; loaded by child Python processes."""

import os
import socket
from pathlib import Path


def require_loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host not in ("127.0.0.1", "localhost"):
        message = f"Loopback guard refused host: {host!r}"
        log = os.environ.get("LOOPBACK_GUARD_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as output:
                output.write(message + "\n")
        raise RuntimeError(message)


_getaddrinfo = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def guarded_getaddrinfo(host, *args, **kwargs):
    require_loopback(host)
    return _getaddrinfo(host, *args, **kwargs)


def guarded_connect(sock, address):
    require_loopback(address[0])
    return _connect(sock, address)


def guarded_connect_ex(sock, address):
    require_loopback(address[0])
    return _connect_ex(sock, address)


socket.getaddrinfo = guarded_getaddrinfo
socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect_ex

if ready := os.environ.get("LOOPBACK_GUARD_READY"):
    Path(ready).write_text("loopback guard installed\n", encoding="utf-8")
if log := os.environ.get("LOOPBACK_GUARD_LOG"):
    Path(log).touch()
