"""Refuse external DNS and socket connections before they can reach the network."""

import os
import socket
from pathlib import Path


def _check(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host not in ("127.0.0.1", "localhost"):
        log = os.environ.get("LOOPBACK_GUARD_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as output:
                output.write("Refused non-loopback host\n")
        raise OSError("Loopback guard refused non-loopback host")


_original_getaddrinfo = socket.getaddrinfo
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


def _getaddrinfo(host, *args, **kwargs):
    _check(host)
    return _original_getaddrinfo(host, *args, **kwargs)


def _connect(sock, address):
    _check(address[0] if isinstance(address, tuple) else None)
    return _original_connect(sock, address)


def _connect_ex(sock, address):
    _check(address[0] if isinstance(address, tuple) else None)
    return _original_connect_ex(sock, address)


socket.getaddrinfo = _getaddrinfo
socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex

if os.environ.get("LOOPBACK_GUARD_LOG"):
    Path(os.environ["LOOPBACK_GUARD_LOG"]).touch()
if os.environ.get("LOOPBACK_GUARD_READY"):
    Path(os.environ["LOOPBACK_GUARD_READY"]).write_text("installed\n", encoding="utf-8")
