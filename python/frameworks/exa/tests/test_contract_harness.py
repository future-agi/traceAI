"""Shared-harness contract test for traceAI-exa (TH-8324, TH-8339 Receiver).

The real ``exa-py`` client makes real HTTP calls to a loopback fake of the
Exa API (``Exa(base_url=...)``). Spans leave through the real
``fi_instrumentation.register()`` OTLP exporter into ``harness.Receiver``.
Nothing calls Exa; every key is a placeholder.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes  # noqa: E402

FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
EXA_KEY = "placeholder-exa-key-must-not-be-exported"
PROJECT = "exa-contract"
# Response text the fake API returns. None of it may reach a span.
RESULT_TITLE = "RESULT-TITLE-MUST-NOT-BE-EXPORTED"
ANSWER_TEXT = "ANSWER-TEXT-MUST-NOT-BE-EXPORTED"

_ROUTES: Dict[Tuple[str, str], Tuple[int, Dict[str, Any]]] = {
    ("POST", "/search"): (
        200,
        {
            "requestId": "req-1",
            "results": [
                {"id": "doc-1", "url": "https://example.com/a", "title": RESULT_TITLE},
                {"id": "doc-2", "url": "https://example.com/b", "title": RESULT_TITLE},
            ],
        },
    ),
    ("POST", "/answer"): (
        200,
        {
            "answer": ANSWER_TEXT,
            "citations": [{"id": "doc-1", "url": "https://example.com/a", "title": RESULT_TITLE}],
        },
    ),
}


class FakeExa:
    """Loopback stand-in for api.exa.ai. A query of ``fail-401`` gets HTTP 401."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                owner.calls.append(("POST", path, body))
                status, payload = _ROUTES.get(("POST", path), (404, {"error": "not found"}))
                if body.get("query") == "fail-401":
                    status, payload = 401, {"error": "invalid api key"}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


@pytest.fixture()
def fake_exa():
    server = FakeExa()
    yield server
    server.close()


def _journey(receiver: Receiver, fake: FakeExa, monkeypatch: pytest.MonkeyPatch) -> List[dict]:
    from exa_py import Exa
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    from traceai_exa import ExaInstrumentor

    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
    provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)
    instrumentor = ExaInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        exa = Exa(api_key=EXA_KEY, base_url=fake.origin)
        assert len(exa.search("open telemetry retrieval", num_results=2).results) == 2
        assert exa.answer("what is OTLP?").answer == ANSWER_TEXT
        with pytest.raises(ValueError):
            exa.search("fail-401")
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    return receiver.spans()


def test_real_client_calls_reach_the_collector_contract(fake_exa, monkeypatch):
    with Receiver() as receiver:
        spans = _journey(receiver, fake_exa, monkeypatch)
        exports = receiver.requests()

    # The real SDK made the HTTP calls (no monkeypatched client methods).
    assert [(m, p) for m, p, _ in fake_exa.calls] == [
        ("POST", "/search"),
        ("POST", "/answer"),
        ("POST", "/search"),
    ]

    # Collector contract on every export.
    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert "authorization" not in export["headers"]
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"

    by_name: Dict[str, List[dict]] = {}
    for span in spans:
        by_name.setdefault(span["name"], []).append(span)
    assert sorted((name, len(group)) for name, group in by_name.items()) == [
        ("exa.answer", 1),
        ("exa.search", 2),
    ]

    ok_search, failed_search = by_name["exa.search"]
    ok = _flatten_attributes(ok_search["attributes"])
    assert ok["fi.span.kind"] == "RETRIEVER"
    assert ok["fi.retrieval.query"] == "open telemetry retrieval"
    assert ok["input.value"] == "open telemetry retrieval"
    assert int(ok["fi.retrieval.document_count"]) == 2
    assert ok_search.get("status", {}).get("code") in (None, "STATUS_CODE_OK", "STATUS_CODE_UNSET")

    answer = _flatten_attributes(by_name["exa.answer"][0]["attributes"])
    assert answer["fi.span.kind"] == "RETRIEVER"
    assert not any("model" in key for key in answer)

    failed = _flatten_attributes(failed_search["attributes"])
    assert failed["fi.retrieval.query"] == "fail-401"
    assert failed_search["status"]["code"] == "STATUS_CODE_ERROR"
    assert any(event["name"] == "exception" for event in failed_search.get("events", []))


def test_no_key_or_response_content_is_exported(fake_exa, monkeypatch):
    with Receiver() as receiver:
        spans = _journey(receiver, fake_exa, monkeypatch)
        exports = receiver.requests()

    wire = json.dumps(spans) + json.dumps(exports)
    for secret in (EXA_KEY, RESULT_TITLE, ANSWER_TEXT, "https://example.com/a"):
        assert secret not in wire, secret
