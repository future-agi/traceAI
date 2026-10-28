"""Standard-library HTTP server for the ElevenLabs post-call webhook.

``POST {path}`` (default ``/elevenlabs/webhook``) handles one delivery.
``GET /healthz`` answers 200. Anything else is 404/405.

Put TLS in front of it (a reverse proxy or your platform's HTTPS ingress):
ElevenLabs delivers to an HTTPS URL. Request logs carry the method, path and
status only; the forwarder's own log lines carry the conversation id and the
outcome only.

A body is read into memory (at most ``max_body_bytes``; a larger declared length is
refused with 413 unread) before its signature can be checked, because the HMAC
covers the raw bytes. A delivery whose signature fails is never parsed. Rate- and
size-limit unauthenticated traffic at your ingress.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

from .forwarder import Forwarder
from .verify import SIGNATURE_HEADER

logger = logging.getLogger("traceai_elevenlabs_agents")

DEFAULT_PATH = "/elevenlabs/webhook"
#: ElevenLabs truncates tool attributes at 4 KB each; a whole conversation is far below this.
DEFAULT_MAX_BODY_BYTES = 20 * 1024 * 1024


def make_server(
    forwarder: Forwarder,
    host: str = "127.0.0.1",
    port: int = 8787,
    path: str = DEFAULT_PATH,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "traceai-elevenlabs-agents"
        sys_version = ""

        def _reply(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path == "/healthz":
                self._reply(200, {"status": "ok"})
            else:
                self._reply(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != path:
                self._reply(404, {"error": "not_found"})
                return
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                self._reply(411, {"error": "length_required"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._reply(411, {"error": "length_required"})
                return
            if length < 0 or length > max_body_bytes:
                self._reply(413, {"error": "payload_too_large"})
                return
            raw_body = self.rfile.read(length)
            try:
                status, payload = forwarder.handle_webhook(raw_body, self.headers.get(SIGNATURE_HEADER))
            except Exception:  # noqa: BLE001 - never leak a traceback to the caller
                logger.error("unexpected error while handling a delivery")
                status, payload = 500, {"error": "internal_error"}
            self._reply(status, payload)

        def do_PUT(self) -> None:  # noqa: N802
            self._reply(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

        do_DELETE = do_PATCH = do_PUT

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Default logging prints the request line with the full query string.
            # Keep method, bare path and status only.
            return

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            logger.info("%s %s -> %s", self.command, urlsplit(self.path).path, code)

    return ThreadingHTTPServer((host, port), Handler)


def serve(
    forwarder: Forwarder,
    host: str = "127.0.0.1",
    port: int = 8787,
    path: str = DEFAULT_PATH,
    ready: Optional[threading.Event] = None,
) -> None:
    """Run until SIGTERM/SIGINT. In-flight requests finish before the process exits."""
    httpd = make_server(forwarder, host, port, path)
    bound: Tuple[str, int] = httpd.server_address[:2]  # type: ignore[assignment]
    logger.info("listening on http://%s:%d%s project=%s", bound[0], bound[1], path, forwarder.config.project_name)

    def _stop(signum: int, _frame: Any) -> None:
        logger.info("signal %d: shutting down", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    httpd.daemon_threads = False
    httpd.block_on_close = True
    if ready is not None:
        ready.set()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        forwarder.store.close()
