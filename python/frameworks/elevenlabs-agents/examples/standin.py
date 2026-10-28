"""STANDIN for ElevenLabs: a local sender and a local GET endpoint, shaped like the docs.

Nothing here talks to ElevenLabs. It exists so the forwarder can be exercised end to
end without an ElevenLabs workspace:

* ``build_delivery`` turns a fixture into a fresh ``post_call_transcription_otel`` body
  (new conversation id, trace id and span ids, timestamps moved to now, a marker in
  every content field).
* ``send`` signs it exactly as documented (``ElevenLabs-Signature: t={unix},v0={hmac}``
  over ``{timestamp}.{body}``) and POSTs it.
* ``GetStandin`` serves ``GET /v1/convai/conversations/{id}?format=opentelemetry`` with
  the same ``otlp_traces`` (same trace id, fresh span ids, as ElevenLabs does on every
  export) and checks the ``xi-api-key`` header.

Fixture provenance: tests/fixtures/README.md.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "tests" / "fixtures"


def _sign(secret: str, body: bytes, timestamp: int) -> str:
    import hashlib
    import hmac

    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return "t=%d,v0=%s" % (timestamp, digest)


def _spans(otlp: Dict[str, Any]):
    for rs in otlp.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                yield span


def refresh_span_ids(otlp: Dict[str, Any]) -> Dict[str, Any]:
    """Same trace id, new span ids: what a second ElevenLabs export of a conversation looks like."""
    otlp = json.loads(json.dumps(otlp))
    mapping: Dict[str, str] = {}
    for span in _spans(otlp):
        mapping[span["spanId"]] = secrets.token_hex(8)
    for span in _spans(otlp):
        span["spanId"] = mapping[span["spanId"]]
        if span.get("parentSpanId"):
            span["parentSpanId"] = mapping.get(span["parentSpanId"], span["parentSpanId"])
    return otlp


def build_delivery(fixture: str, marker: str, *, now: Optional[int] = None) -> Dict[str, Any]:
    text = (FIXTURES / fixture).read_text(encoding="utf-8").replace("{{MARKER}}", marker)
    body = json.loads(text)
    data = body["data"]
    old_conv = data["conversation_id"]
    new_conv = "conv_th8231" + secrets.token_hex(10)
    new_trace = secrets.token_hex(16)
    text = json.dumps(body).replace(old_conv, new_conv)
    body = json.loads(text)
    otlp = refresh_span_ids(body["data"]["otlp_traces"])
    spans = list(_spans(otlp))
    starts = [int(s["startTimeUnixNano"]) for s in spans]
    ends = [int(s["endTimeUnixNano"]) for s in spans]
    current = int(time.time()) if now is None else now
    shift = (current - 5) * 1_000_000_000 - max(ends)
    for span in spans:
        span["traceId"] = new_trace
        span["startTimeUnixNano"] = str(int(span["startTimeUnixNano"]) + shift)
        span["endTimeUnixNano"] = str(int(span["endTimeUnixNano"]) + shift)
        for event in span.get("events", []):
            event["timeUnixNano"] = str(int(event["timeUnixNano"]) + shift)
    del starts
    body["data"]["otlp_traces"] = otlp
    body["event_timestamp"] = current
    return body


def send(
    url: str,
    body: Any,
    secret: str,
    *,
    timestamp: Optional[int] = None,
    header: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    raw = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode("utf-8")
    ts = int(time.time()) if timestamp is None else timestamp
    headers = {"Content-Type": "application/json"}
    signature = _sign(secret, raw, ts) if header is None else header
    if signature:
        headers["ElevenLabs-Signature"] = signature
    request = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


class GetStandin:
    """Loopback ``GET /v1/convai/conversations/{id}`` shaped like the ElevenLabs API."""

    def __init__(self, api_key: str, host: str = "127.0.0.1", port: int = 0) -> None:
        self.api_key = api_key
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.requests: list = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                owner.requests.append({"path": parts.path, "query": query})
                if self.headers.get("xi-api-key") != owner.api_key:
                    self._json(401, {"detail": {"status": "invalid_api_key"}})
                    return
                prefix = "/v1/convai/conversations/"
                conv_id = parts.path[len(prefix):] if parts.path.startswith(prefix) else ""
                conversation = owner.conversations.get(conv_id)
                if conversation is None:
                    self._json(404, {"detail": {"status": "conversation_not_found"}})
                    return
                payload = dict(conversation)
                if query.get("format", ["json"])[0] == "opentelemetry":
                    payload["otlp_traces"] = refresh_span_ids(conversation["otlp_traces"])
                else:
                    payload.pop("otlp_traces", None)
                self._json(200, payload)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.base_url = "http://%s:%d" % (host, self.httpd.server_port)
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def add_from_delivery(self, delivery: Dict[str, Any], *, user_id: Optional[str] = None) -> str:
        data = delivery["data"]
        template = json.loads((FIXTURES / "get_conversation_opentelemetry.json").read_text(encoding="utf-8"))
        template.update(
            conversation_id=data["conversation_id"],
            agent_id=data["agent_id"],
            otlp_traces=data["otlp_traces"],
        )
        if user_id is None:
            template.pop("user_id", None)
        else:
            template["user_id"] = user_id
        self.conversations[data["conversation_id"]] = template
        return data["conversation_id"]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def unique_marker() -> str:
    return "zqx-th8231-" + secrets.token_hex(6) + "-" + str(os.getpid())
