"""A loopback stand-in for api.tavily.com, shared by every Tavily test.

``FakeTavily`` speaks enough of the Tavily HTTP API for tavily-python 0.8.4
(``TavilyClient`` / ``AsyncTavilyClient``) and for LangChain's Tavily tool
(``langchain_community`` posts to the same ``/search`` route). It listens on
127.0.0.1 only and every key a test uses is a placeholder.

Scripted queries (search) and URLs (extract):

* ``fail-401`` gets HTTP 401 (``InvalidAPIKeyError``).
* ``fail-500`` gets HTTP 500 (``requests.HTTPError`` / ``httpx.HTTPStatusError``).
* ``keyless-limit`` gets HTTP 429 with the keyless error envelope
  (``TavilyKeylessLimitError`` on a keyless client).
* a query starting ``echo-400`` gets HTTP 400 whose detail repeats the query,
  so an error message can carry whatever the query carried.
* ``slow`` (a query, or a URL containing it) is held until the fake closes,
  so a caller can cancel mid-call.
* an extract URL containing ``fail`` is returned in ``failed_results``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

TAVILY_KEY = "tvly-placeholder-key-must-not-be-exported"
FAIL_401 = "fail-401"
FAIL_500 = "fail-500"
KEYLESS_LIMIT = "keyless-limit"
ECHO_400 = "echo-400"
SLOW = "slow"

# Response content the fake returns. None of it may reach a span.
RESULT_TITLE = "RESULT-TITLE-MUST-NOT-BE-EXPORTED"
RESULT_CONTENT = "RESULT-CONTENT-MUST-NOT-BE-EXPORTED"
RAW_CONTENT = "RAW-CONTENT-MUST-NOT-BE-EXPORTED"
ANSWER_TEXT = "ANSWER-TEXT-MUST-NOT-BE-EXPORTED"
EXTRACT_ERROR = "EXTRACT-ERROR-MUST-NOT-BE-EXPORTED"
CONTENT_MARKERS = (RESULT_TITLE, RESULT_CONTENT, RAW_CONTENT, ANSWER_TEXT, EXTRACT_ERROR)

DEFAULT_SEARCH_RESULTS = 2


def _result(index: int) -> Dict[str, Any]:
    return {
        "url": "https://example.com/result-{0}".format(index),
        "title": RESULT_TITLE,
        "content": RESULT_CONTENT,
        "score": 0.9,
        "raw_content": RAW_CONTENT,
    }


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A cancelled client leaves a broken pipe behind; that is expected.
        return


class FakeTavily:
    """Loopback Tavily API. ``calls`` records (path, lower-cased headers, body)."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, str], Dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                headers = {key.lower(): value for key, value in self.headers.items()}
                with owner._lock:
                    owner.calls.append((path, headers, body))
                if path == "/search":
                    self._search(body)
                elif path == "/extract":
                    self._extract(body)
                else:
                    self._send(404, {"detail": {"error": "not found"}})

            def _search(self, body: Dict[str, Any]) -> None:
                query = str(body.get("query", ""))
                if query == FAIL_401:
                    self._send(401, {"detail": {"error": "Unauthorized: invalid API key."}})
                elif query == FAIL_500:
                    self._send(500, {"detail": {"error": "internal error"}})
                elif query == KEYLESS_LIMIT:
                    self._send(
                        429,
                        {
                            "error": {
                                "code": "keyless_rate_limited",
                                "message": "Keyless limit reached.",
                                "window": "day",
                                "retry_after_seconds": 60,
                                "next_actions": [],
                            }
                        },
                    )
                elif query.startswith(ECHO_400):
                    self._send(400, {"detail": {"error": "Bad query: {0}".format(query)}})
                elif query == SLOW:
                    owner._release.wait(30)
                    self._send(200, {"query": query, "results": []})
                else:
                    count = int(body.get("max_results") or DEFAULT_SEARCH_RESULTS)
                    self._send(
                        200,
                        {
                            "query": query,
                            "answer": ANSWER_TEXT,
                            "images": [],
                            "follow_up_questions": None,
                            "results": [_result(i) for i in range(count)],
                            "response_time": 0.01,
                            "request_id": "req-search-1",
                        },
                    )

            def _extract(self, body: Dict[str, Any]) -> None:
                urls = body.get("urls", [])
                if isinstance(urls, str):
                    urls = [urls]
                if any(SLOW in str(url) for url in urls):
                    owner._release.wait(30)
                results = []
                failed = []
                for url in urls:
                    if "fail" in str(url):
                        failed.append({"url": url, "error": EXTRACT_ERROR})
                    else:
                        results.append({"url": url, "raw_content": RAW_CONTENT, "images": []})
                self._send(
                    200,
                    {
                        "results": results,
                        "failed_results": failed,
                        "response_time": 0.01,
                        "request_id": "req-extract-1",
                    },
                )

            def _send(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_: Any) -> None:
                return

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def paths(self) -> List[str]:
        with self._lock:
            return [path for path, _, _ in self.calls]

    def bodies(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [body for _, _, body in self.calls]

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def __enter__(self) -> "FakeTavily":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
