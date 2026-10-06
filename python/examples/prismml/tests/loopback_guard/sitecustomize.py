"""Refuse non-loopback subprocess networking before DNS or socket connection."""

import os
from pathlib import Path
import socket


def _check(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host not in ("127.0.0.1", "localhost"):
        with open(os.environ["LOOPBACK_GUARD_LOG"], "a", encoding="utf-8") as log:
            log.write("BLOCKED non-loopback socket operation\n")
        raise RuntimeError("loopback guard refused a non-loopback host before DNS/connect")


_getaddrinfo = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _guarded_getaddrinfo(host, *args, **kwargs):
    _check(host)
    return _getaddrinfo(host, *args, **kwargs)


def _guarded_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _check(address[0])
    return _connect(sock, address)


def _guarded_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _check(address[0])
    return _connect_ex(sock, address)


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
Path(os.environ["LOOPBACK_GUARD_READY"]).write_text("installed\n", encoding="utf-8")
