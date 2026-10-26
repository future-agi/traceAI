"""Per-conversation idempotency record, kept in the customer's process (SQLite file).

ElevenLabs keeps the trace id stable per conversation but mints fresh span ids on
every export, so forwarding the same conversation twice (a webhook retry after a
lost 2xx, or a GET backfill after the webhook) would duplicate every turn and tool
span inside one trace. The forwarder therefore records each conversation it
forwarded and acknowledges a repeat without exporting again.

A row is ``pending`` while one worker exports it and ``done`` after the collector
accepted the batch. A failed export deletes the row so the next delivery retries.
A ``pending`` row older than ``stale_after_secs`` is taken over (the worker that
claimed it died before finishing).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional

CLAIMED = "claimed"
DUPLICATE = "duplicate"
IN_FLIGHT = "in_flight"


class ForwardedStore:
    def __init__(self, path: str, stale_after_secs: int = 300) -> None:
        self.path = path
        self.stale_after_secs = stale_after_secs
        self._lock = threading.Lock()
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        # One connection per store; the lock serialises threads of this process,
        # SQLite's own locking serialises processes sharing the file.
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS forwarded ("
            " conversation_id TEXT PRIMARY KEY,"
            " state TEXT NOT NULL,"
            " source TEXT NOT NULL,"
            " trace_id TEXT,"
            " updated_at REAL NOT NULL)"
        )

    def claim(self, conversation_id: str, source: str, *, force: bool = False) -> str:
        """Return CLAIMED (caller must export, then ``complete`` or ``release``), DUPLICATE or IN_FLIGHT."""
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT state, updated_at FROM forwarded WHERE conversation_id = ?", (conversation_id,)
                ).fetchone()
                if row is not None:
                    state, updated_at = row
                    if state == "done" and not force:
                        self._conn.execute("COMMIT")
                        return DUPLICATE
                    if state == "pending" and now - updated_at < self.stale_after_secs:
                        self._conn.execute("COMMIT")
                        return IN_FLIGHT
                self._conn.execute(
                    "INSERT INTO forwarded (conversation_id, state, source, trace_id, updated_at)"
                    " VALUES (?, 'pending', ?, NULL, ?)"
                    " ON CONFLICT(conversation_id) DO UPDATE SET state='pending', source=excluded.source,"
                    " updated_at=excluded.updated_at",
                    (conversation_id, source, now),
                )
                self._conn.execute("COMMIT")
                return CLAIMED
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def complete(self, conversation_id: str, trace_id: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE forwarded SET state='done', trace_id=?, updated_at=? WHERE conversation_id=?",
                (trace_id, time.time(), conversation_id),
            )

    def release(self, conversation_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM forwarded WHERE conversation_id=? AND state='pending'", (conversation_id,)
            )

    def state(self, conversation_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM forwarded WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
