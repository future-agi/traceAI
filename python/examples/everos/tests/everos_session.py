"""Test scenario: one memory add, flush and search through EverOS's own app.

Usage: python everos_session.py "<question>"

Runs ``everos init`` for ``EVEROS_ROOT``, then drives EverOS's FastAPI app
(``everos.entrypoints.api.app.create_app``, the app ``everos server start``
serves) in-process with Starlette's ``TestClient``: no port is bound and no
HTTP server or wrapper is started. The app's own lifespan reads the
``[observability]`` settings from the environment and installs EverOS's
tracer, exactly as the server does. Not part of the recipe.

Steps: ``POST /api/v2/memory/add`` (three messages), ``POST
/api/v2/memory/flush`` (forces boundary detection and extraction), poll
``POST /api/v2/memory/get`` until the episode is indexed (``/get`` opens no
span), wait for the background strategies, then ``POST
/api/v2/memory/search`` (hybrid). Leaving the ``TestClient`` block runs the
lifespan shutdown, which flushes the spans. The last stdout line is JSON.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import tiktoken
import tiktoken.registry

# everalgo counts tokens with tiktoken's o200k_base, whose BPE file tiktoken
# downloads from openaipublic.blob.core.windows.net on first use. Register a
# byte-level stand-in under that name so the scenario stays offline. Token
# counts only feed everalgo's boundary limits, never a span.
tiktoken.registry.ENCODINGS["o200k_base"] = tiktoken.Encoding(
    name="o200k_base",
    pat_str=r"\S+|\s+",
    mergeable_ranks={bytes([i]): i for i in range(256)},
    special_tokens={},
)

from fastapi.testclient import TestClient  # noqa: E402

from _scenario import MESSAGE_MARKER, SESSION_ID, USER_ID  # noqa: E402
from everos.entrypoints.api.app import create_app  # noqa: E402

INDEX_TIMEOUT_SECONDS = 60.0
# Background strategies (atomic facts, profile clustering) run on their own
# scheduler; with the loopback fake they finish well inside this.
SETTLE_SECONDS = 3.0


def main() -> None:
    question = sys.argv[1]
    root = os.environ["EVEROS_ROOT"]
    everos_cli = os.path.join(os.path.dirname(sys.executable), "everos")
    init = subprocess.run([everos_cli, "init", "--root", root], capture_output=True, text=True)
    if init.returncode != 0:
        raise SystemExit("everos init failed: " + init.stderr[-2000:])

    now_ms = int(time.time() * 1000)
    messages = [
        {
            "sender_id": USER_ID,
            "role": "user",
            "timestamp": now_ms - 60000,
            "content": "Hi, I am Ada. I work on the Lighthouse project. " + MESSAGE_MARKER,
        },
        {
            "sender_id": "assistant",
            "role": "assistant",
            "timestamp": now_ms - 50000,
            "content": "Nice to meet you, Ada.",
        },
        {
            "sender_id": USER_ID,
            "role": "user",
            "timestamp": now_ms - 40000,
            "content": "Lighthouse ships next week.",
        },
    ]
    result: dict = {}
    with TestClient(create_app()) as client:
        add = client.post(
            "/api/v2/memory/add", json={"session_id": SESSION_ID, "messages": messages}
        )
        result["add"] = [add.status_code, add.json().get("data")]
        flush = client.post("/api/v2/memory/flush", json={"session_id": SESSION_ID})
        result["flush"] = [flush.status_code, flush.json().get("data")]

        started = time.monotonic()
        indexed = 0
        while time.monotonic() - started < INDEX_TIMEOUT_SECONDS:
            got = client.post(
                "/api/v2/memory/get", json={"user_id": USER_ID, "memory_type": "episode"}
            )
            indexed = (got.json().get("data") or {}).get("total_count") or 0
            if indexed:
                break
            time.sleep(0.5)
        result["indexed_episodes"] = indexed
        time.sleep(SETTLE_SECONDS)

        search = client.post(
            "/api/v2/memory/search",
            json={"user_id": USER_ID, "query": question, "method": "hybrid"},
        )
        data = search.json().get("data") or {}
        result["search"] = [search.status_code, [e["id"] for e in data.get("episodes", [])]]
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
