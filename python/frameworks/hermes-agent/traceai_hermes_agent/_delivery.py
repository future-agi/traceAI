"""Bounded metadata-only diagnostics and a send-time disable fence."""
from __future__ import annotations

import datetime
import json
import os
import stat
import tempfile
import threading

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from ._config import permitted, read_json


class Delivery(SpanExporter):
    def __init__(self, exporter: SpanExporter, config: dict):
        self.exporter, self.config = exporter, config
        self._lock = threading.Lock()
        self._state = dict(pending=0, exported=0, dropped=0, activatedAt=self.now(),
                           binding=config["diagnostic_binding"])
        self.record()

    @staticmethod
    def now():
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def record(self, **values):
        # Telemetry state must never wait for another Hermes process. Counters
        # are this process's latest snapshot, not a durable exactly-once ledger.
        with self._lock:
            self._state.update(values)
            path = self.config["state"]
            temporary = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                for parent in path.parents:
                    st = parent.lstat()
                    if not stat.S_ISDIR(st.st_mode) or (st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX):
                        return
                if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
                    return
                read_json(path)  # reject symlink/non-owner/public destination
                fd, temporary = tempfile.mkstemp(prefix=".hermes-", dir=path.parent)
                with os.fdopen(fd, "w") as f:
                    json.dump(self._state, f)
                os.replace(temporary, path)
                temporary = None
            except Exception:
                pass
            finally:
                if temporary:
                    os.unlink(temporary)

    def export(self, spans):
        if not permitted(self.config):
            self.record(dropped=self._state["dropped"] + len(spans), lastError="DISABLED")
            return SpanExportResult.SUCCESS  # deliberately discarded, never retry
        result = self.exporter.export(spans)
        if result == SpanExportResult.SUCCESS:
            self.record(exported=self._state["exported"] + len(spans), lastAcceptedAt=self.now(), lastError=None)
        else:
            self.record(dropped=self._state["dropped"] + len(spans), lastError="EXPORT_NOT_ACCEPTED")
        return result

    def shutdown(self):
        return self.exporter.shutdown()

    def force_flush(self, timeout_millis=2000):
        return self.exporter.force_flush(timeout_millis)
