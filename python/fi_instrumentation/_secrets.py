"""Process-wide known secrets. Never expose values through diagnostics or reprs."""

import os
import threading
from collections import OrderedDict
from typing import Tuple

from fi_instrumentation._privacy_catalog import credential_name

_LIMIT = 1024


class _SecretRegistry:
    """Writers serialize LRU updates; readers take one immutable snapshot lookup.

    Recency means last registration/transport observation, not last exported span.
    Sorting happens only when membership changes, never on unrelated span paths.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: OrderedDict = OrderedDict()
        self._snapshot: Tuple[str, ...] = ()

    def register(self, value: str) -> None:
        # No coercion, subclass hooks, logging, or errors containing the value.
        if type(value) is not str or len(value) < 8:
            return
        with self._lock:
            if value in self._values:
                self._values.move_to_end(value)
                return
            self._values[value] = None
            if len(self._values) > _LIMIT:
                self._values.popitem(last=False)
            self._snapshot = tuple(sorted(self._values, key=len, reverse=True))

    def snapshot(self) -> Tuple[str, ...]:
        return self._snapshot

    def _after_fork(self) -> None:
        # A vanished parent thread may have held the lock. Keep inherited secrets.
        self._lock = threading.Lock()


_registry = _SecretRegistry()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_registry._after_fork)


def register_secret(value: str) -> None:
    """Register an extra credential for all agent-exported spans in this process.

    Only plain strings of at least eight characters are retained. The registry
    holds at most 1024 values, evicting the least recently registered/observed.
    This never changes application data or request authentication.
    """
    _registry.register(value)


def snapshot() -> Tuple[str, ...]:
    return _registry.snapshot()


def seed_environment() -> None:
    """Called at initialization, not on the per-span export path."""
    for name, value in os.environ.copy().items():
        if credential_name(name):
            register_secret(value)
