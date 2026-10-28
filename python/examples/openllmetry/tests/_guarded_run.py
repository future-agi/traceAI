"""Run a script with every non-loopback network connection blocked and logged.

Usage: python _guarded_run.py <script.py> [args...]

Before the script starts, ``socket.socket.connect``/``connect_ex`` and
``socket.getaddrinfo`` refuse any host other than 127.0.0.1, ::1 or
localhost. Each refused attempt is appended as a JSON line to the file named
by ``LOOPBACK_GUARD_LOG``, so a test can assert that the SDK, its
instrumentors and the example tried to reach nothing else (no Traceloop host,
no telemetry endpoint, no real OpenAI). Unix sockets are not affected.
"""

from __future__ import annotations

import json
import os
import runpy
import socket
import sys

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}
_LOG_PATH = os.environ.get("LOOPBACK_GUARD_LOG", "")


def _host(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return str(value)


def _refuse(kind: str, target: object) -> None:
    if _LOG_PATH:
        with open(_LOG_PATH, "a", encoding="utf-8") as log:
            log.write(json.dumps({"kind": kind, "target": repr(target)}) + "\n")


_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo


def _is_blocked(sock: socket.socket, address: object) -> bool:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return False
    host = address[0] if isinstance(address, tuple) else address
    return _host(host) not in _ALLOWED_HOSTS


def _connect(self: socket.socket, address: object) -> None:
    if _is_blocked(self, address):
        _refuse("connect", address)
        raise OSError("blocked by loopback guard: {0!r}".format(address))
    return _original_connect(self, address)


def _connect_ex(self: socket.socket, address: object) -> int:
    if _is_blocked(self, address):
        _refuse("connect_ex", address)
        raise OSError("blocked by loopback guard: {0!r}".format(address))
    return _original_connect_ex(self, address)


def _getaddrinfo(host: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
    if host is not None and _host(host) not in _ALLOWED_HOSTS:
        _refuse("getaddrinfo", host)
        raise socket.gaierror("blocked by loopback guard: {0!r}".format(host))
    return _original_getaddrinfo(host, *args, **kwargs)


def main() -> None:
    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = _getaddrinfo  # type: ignore[assignment]
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
