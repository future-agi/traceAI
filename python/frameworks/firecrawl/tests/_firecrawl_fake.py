"""Loopback fake of the Firecrawl v2 HTTP API for traceAI-firecrawl tests.

The real ``firecrawl-py`` clients (sync ``requests`` and async ``httpx``) talk to
this server through ``api_url``. Nothing reaches api.firecrawl.dev, and every
key used against it is a placeholder.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Tuple, Union

JOB_ID = "crawl-job-1"
PAGE_BODY = "PAGE-BODY-MUST-NOT-BE-EXPORTED"
RESULT_TITLE = "RESULT-TITLE-MUST-NOT-BE-EXPORTED"

PAGE = {"markdown": PAGE_BODY, "metadata": {"sourceURL": "https://example.com/1", "statusCode": 200}}

Payload = Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]
Route = Tuple[int, Payload]


def crawl_status(status: str, pages: int = 3) -> Dict[str, Any]:
    """Body of ``GET /v2/crawl/{id}`` for a job in ``status``."""
    return {
        "success": True,
        "status": status,
        "total": pages,
        "completed": pages,
        "creditsUsed": pages,
        "expiresAt": "2030-01-01T00:00:00Z",
        "data": [PAGE] * pages,
    }


def default_routes() -> Dict[Tuple[str, str], Route]:
    return {
        ("POST", "/v2/scrape"): (200, {"success": True, "data": PAGE}),
        ("POST", "/v2/search"): (
            200,
            {
                "success": True,
                "data": {
                    "web": [
                        {"url": "https://example.com/a", "title": RESULT_TITLE, "description": PAGE_BODY},
                        {"url": "https://example.com/b", "title": RESULT_TITLE, "description": PAGE_BODY},
                    ]
                },
            },
        ),
        ("POST", "/v2/map"): (
            200,
            {
                "success": True,
                "links": [
                    {"url": "https://example.com/private/a", "title": RESULT_TITLE},
                    {"url": "https://example.com/private/b", "title": RESULT_TITLE},
                ],
            },
        ),
        ("POST", "/v2/crawl"): (200, {"success": True, "id": JOB_ID, "url": "unused"}),
        ("GET", "/v2/crawl/" + JOB_ID): (200, crawl_status("completed")),
        ("DELETE", "/v2/crawl/" + JOB_ID): (200, {"success": True, "status": "cancelled"}),
    }


class FakeFirecrawl:
    """Threaded loopback HTTP server that answers like the Firecrawl v2 API.

    ``routes`` maps ``(method, path)`` to ``(status, payload)``. A payload may be
    a callable that receives the decoded request body. Unknown routes get 404.
    """

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Route] = default_routes()
        self.calls: List[Tuple[str, str]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                path = self.path.split("?")[0]
                with owner._lock:
                    owner.calls.append((method, path))
                    status, payload = owner.routes.get(
                        (method, path), (404, {"success": False, "error": "not found"})
                    )
                if callable(payload):
                    payload = payload(body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                self._respond("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._respond("GET")

            def do_DELETE(self) -> None:  # noqa: N802
                self._respond("DELETE")

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def calls_to(self, method: str, path: str) -> int:
        with self._lock:
            return self.calls.count((method, path))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
