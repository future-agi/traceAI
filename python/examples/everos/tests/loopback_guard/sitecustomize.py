"""Block and log every non-loopback network connection, in every test process.

The test scenarios put this directory first on ``PYTHONPATH``, so Python's
``site`` imports this file at the start of each interpreter they start: the
scenario script and the ``everos init`` process it runs (it inherits the
environment). This file then runs the interpreter's own ``sitecustomize``, if
it has one (Homebrew's Python does), so nothing else changes.

When ``LOOPBACK_GUARD_LOG`` is set, ``socket.socket.connect``/``connect_ex``
and ``socket.getaddrinfo`` refuse any host other than 127.0.0.1, ::1 or
localhost. Each refused attempt, and each process the guard is installed in,
is appended as a JSON line to that file, so a test can assert that EverOS,
its everalgo dependencies and the scenario tried to reach nothing else (no
LLM or embedding provider, no tokenizer download, no Langfuse, no Future
AGI). Unix sockets are not affected, and neither is native code that opens
sockets itself.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import socket
import sys

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}
_LOG_PATH = os.environ.get("LOOPBACK_GUARD_LOG", "")


def _host(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return str(value)


def _log(record: dict) -> None:
    with open(_LOG_PATH, "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")


def _refuse(kind: str, target: object) -> None:
    _log({"kind": kind, "target": repr(target), "pid": os.getpid()})


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


def _run_shadowed_sitecustomize() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    rest = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is not None and spec.loader is not None:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


if _LOG_PATH:
    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = _getaddrinfo  # type: ignore[assignment]
    _log({"kind": "installed", "pid": os.getpid(), "argv": list(sys.argv)})
_run_shadowed_sitecustomize()
