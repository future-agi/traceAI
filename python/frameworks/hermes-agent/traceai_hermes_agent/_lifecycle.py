"""Wall-clock shutdown bounds, independent of OpenTelemetry's queue length."""
from __future__ import annotations

import threading
import time

import requests
from opentelemetry.sdk.trace import TracerProvider


class ExportBudget:
    def __init__(self):
        self.deadline = float("inf")

    def limit(self, deadline: float):
        self.deadline = min(self.deadline, deadline)

    def timeout(self, requested: float) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise requests.RequestException("EXPORT_DEADLINE_EXCEEDED")
        return min(requested, remaining)


class BoundedTracerProvider(TracerProvider):
    """Abandon a drain at its deadline; never join the SDK worker unboundedly.

    The SDK may synchronously drain every queued batch during shutdown, and some
    versions also block in force_flush. Only daemon workers call these methods.
    The HTTP budget fences every request/retry once shutdown's deadline expires.
    An already in-flight request may finish later, but cannot start another send.
    """

    def __init__(self, *, budget: ExportBudget, **kwargs):
        super().__init__(shutdown_on_exit=False, **kwargs)
        self._export_budget = budget
        self._lifecycle_lock = threading.Lock()
        self._shutdown_done = None
        self._flush_done = None

    @staticmethod
    def _start(operation):
        done = threading.Event()

        def run():
            try:
                operation()
            except Exception:
                pass  # A background telemetry failure must not reach the host.
            finally:
                done.set()

        threading.Thread(target=run, daemon=True, name="futureagi-export-drain").start()
        return done

    def shutdown(self, timeout_millis=3000):
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        with self._lifecycle_lock:
            self._export_budget.limit(deadline)
            if self._shutdown_done is None:
                self._shutdown_done = self._start(super().shutdown)
            done = self._shutdown_done
        done.wait(max(0, deadline - time.monotonic()))

    def force_flush(self, timeout_millis=2000):
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        with self._lifecycle_lock:
            if self._shutdown_done is not None:
                done = self._shutdown_done
            else:
                if self._flush_done is None or self._flush_done.is_set():
                    flush = super().force_flush
                    self._flush_done = self._start(lambda: flush(timeout_millis=timeout_millis))
                done = self._flush_done
        return done.wait(max(0, deadline - time.monotonic()))
