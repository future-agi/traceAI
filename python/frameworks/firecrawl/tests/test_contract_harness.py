"""Shared-harness contract test for traceAI-firecrawl (TH-8325, TH-8339 Receiver).

The real ``firecrawl-py`` v2 client makes real HTTP calls to a loopback fake
of the Firecrawl API (``Firecrawl(api_url=...)``), including the crawl status
poll. Spans leave through the real ``fi_instrumentation.register()`` OTLP
exporter into ``harness.Receiver``. Nothing calls Firecrawl; every key is a
placeholder.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

pytest.importorskip("firecrawl", reason="firecrawl-py must be installed to test its instrumentor")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes  # noqa: E402

FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
FIRECRAWL_KEY = "fc-placeholder-key-must-not-be-exported"
PROJECT = "firecrawl-contract"
# Content the fake API returns or the caller sends. None of it may reach a span.
PAGE_BODY = "PAGE-BODY-MUST-NOT-BE-EXPORTED"
RESULT_TITLE = "RESULT-TITLE-MUST-NOT-BE-EXPORTED"
SECRET_PATH = "/private/path-must-not-be-exported"

_PAGE = {"markdown": PAGE_BODY, "metadata": {"sourceURL": "https://example.com/1", "statusCode": 200}}
_ROUTES: Dict[Tuple[str, str], Tuple[int, Dict[str, Any]]] = {
    ("POST", "/v2/scrape"): (200, {"success": True, "data": _PAGE}),
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
    ("POST", "/v2/crawl"): (200, {"success": True, "id": "crawl-job-1", "url": "unused"}),
    ("GET", "/v2/crawl/crawl-job-1"): (
        200,
        {
            "success": True,
            "status": "completed",
            "total": 3,
            "completed": 3,
            "creditsUsed": 3,
            "expiresAt": "2030-01-01T00:00:00Z",
            "data": [_PAGE, _PAGE, _PAGE],
        },
    ),
}


class FakeFirecrawl:
    """Loopback stand-in for api.firecrawl.dev. Scraping a ``/fail-500`` URL gets HTTP 500."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                path = self.path.split("?")[0]
                owner.calls.append((method, path))
                status, payload = _ROUTES.get((method, path), (404, {"success": False, "error": "not found"}))
                if str(body.get("url", "")).endswith("/fail-500"):
                    status, payload = 500, {"success": False, "error": "internal error"}
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
def fake_firecrawl():
    server = FakeFirecrawl()
    yield server
    server.close()


def _journey(receiver: Receiver, fake: FakeFirecrawl, monkeypatch: pytest.MonkeyPatch) -> List[dict]:
    from firecrawl import Firecrawl
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    from traceai_firecrawl import FirecrawlInstrumentor

    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
    provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        client = Firecrawl(api_key=FIRECRAWL_KEY, api_url=fake.origin, max_retries=1)
        assert client.scrape("https://example.com" + SECRET_PATH).markdown == PAGE_BODY
        assert len(client.search("open telemetry", limit=2).web) == 2
        job = client.crawl("https://example.com", limit=3, poll_interval=0)
        assert job.status == "completed" and len(job.data) == 3
        with pytest.raises(Exception):
            client.scrape("https://example.com/fail-500")
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    return receiver.spans()


def test_real_client_calls_reach_the_collector_contract(fake_firecrawl, monkeypatch):
    with Receiver() as receiver:
        spans = _journey(receiver, fake_firecrawl, monkeypatch)
        exports = receiver.requests()

    # The real SDK made every HTTP call, including the crawl status poll.
    assert fake_firecrawl.calls == [
        ("POST", "/v2/scrape"),
        ("POST", "/v2/search"),
        ("POST", "/v2/crawl"),
        ("GET", "/v2/crawl/crawl-job-1"),
        ("POST", "/v2/scrape"),
    ]

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert "authorization" not in export["headers"]
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"

    names = [span["name"] for span in spans]
    # One span per call: the crawl's internal status poll does not add a span.
    assert sorted(names) == sorted(
        ["firecrawl.scrape", "firecrawl.search", "firecrawl.crawl", "firecrawl.scrape"]
    )
    by_name: Dict[str, List[dict]] = {}
    for span in spans:
        by_name.setdefault(span["name"], []).append(span)

    for span in spans:
        assert _flatten_attributes(span["attributes"])["fi.span.kind"] == "TOOL"

    crawl = _flatten_attributes(by_name["firecrawl.crawl"][0]["attributes"])
    assert int(crawl["firecrawl.page_count"]) == 3
    assert int(crawl["firecrawl.limit"]) == 3
    assert int(crawl["firecrawl.credits_used"]) == 3
    assert crawl["server.address"] == "example.com"

    search = _flatten_attributes(by_name["firecrawl.search"][0]["attributes"])
    assert search["fi.retrieval.query"] == "open telemetry"
    assert int(search["fi.retrieval.document_count"]) == 2

    ok_scrape, failed_scrape = by_name["firecrawl.scrape"]
    assert _flatten_attributes(ok_scrape["attributes"])["server.address"] == "example.com"
    assert failed_scrape["status"]["code"] == "STATUS_CODE_ERROR"
    assert any(event["name"] == "exception" for event in failed_scrape.get("events", []))


def test_async_client_emits_one_span_per_call(fake_firecrawl, monkeypatch):
    """AsyncFirecrawl.crawl awaits the wrapped start_crawl; that must not add a span."""
    import asyncio

    from firecrawl import AsyncFirecrawl
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    from traceai_firecrawl import FirecrawlInstrumentor

    async def journey() -> None:
        client = AsyncFirecrawl(api_key=FIRECRAWL_KEY, api_url=fake_firecrawl.origin, max_retries=1)
        await client.scrape("https://example.com" + SECRET_PATH)
        job = await client.crawl(url="https://example.com", limit=3, poll_interval=0)
        assert job.status == "completed" and len(job.data) == 3

    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
        monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
        provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)
        instrumentor = FirecrawlInstrumentor()
        instrumentor.instrument(tracer_provider=provider)
        try:
            asyncio.run(journey())
            assert provider.force_flush(timeout_millis=10_000)
        finally:
            instrumentor.uninstrument()
            provider.shutdown()
        spans = receiver.spans()

    assert fake_firecrawl.calls == [
        ("POST", "/v2/scrape"),
        ("POST", "/v2/crawl"),
        ("GET", "/v2/crawl/crawl-job-1"),
    ]
    assert sorted(span["name"] for span in spans) == ["firecrawl.crawl", "firecrawl.scrape"]
    crawl = next(span for span in spans if span["name"] == "firecrawl.crawl")
    assert _flatten_attributes(crawl["attributes"])["firecrawl.job_id"] == "crawl-job-1"


def test_no_key_path_or_content_is_exported(fake_firecrawl, monkeypatch):
    with Receiver() as receiver:
        spans = _journey(receiver, fake_firecrawl, monkeypatch)
        exports = receiver.requests()

    wire = json.dumps(spans) + json.dumps(exports)
    for secret in (FIRECRAWL_KEY, PAGE_BODY, RESULT_TITLE, SECRET_PATH):
        assert secret not in wire, secret


_EXIT_SCRIPT = """
import os
import sys

from firecrawl import Firecrawl
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_firecrawl import FirecrawlInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name=os.environ["PROJECT"], verbose=False)
FirecrawlInstrumentor().instrument(tracer_provider=provider)
Firecrawl(api_key=os.environ["FIRECRAWL_KEY"], api_url=os.environ["FIRECRAWL_BASE_URL"], max_retries=1).scrape(
    "https://example.com"
)
# No force_flush(): the batch processor must export at interpreter exit.
if sys.argv[1] == "hard-exit":
    os._exit(0)
"""


@pytest.mark.parametrize("exit_mode,exported", [("normal", True), ("hard-exit", False)])
def test_scrape_span_is_exported_at_process_exit(fake_firecrawl, exit_mode, exported):
    """AC-09: a script that exits normally exports its span without an explicit
    flush (register() uses a batch processor). The hard-exit control skips the
    exit hooks and must export nothing, which shows the receiver is not fed any
    other way."""
    import os

    from harness import run

    with Receiver() as receiver:
        env = dict(os.environ)
        here = Path(__file__).resolve()
        # The package and the in-repo fi_instrumentation, whatever the cwd.
        env["PYTHONPATH"] = os.pathsep.join(
            [str(here.parents[1]), str(here.parents[3])] + [p for p in [env.get("PYTHONPATH")] if p]
        )
        env.update(
            FI_BASE_URL=receiver.origin,
            FI_API_KEY=FI_API_KEY,
            FI_SECRET_KEY=FI_SECRET_KEY,
            PROJECT=PROJECT,
            FIRECRAWL_KEY=FIRECRAWL_KEY,
            FIRECRAWL_BASE_URL=fake_firecrawl.origin,
        )
        result = run([sys.executable, "-c", _EXIT_SCRIPT, exit_mode], env=env, stdin=None, timeout=60)
        spans = receiver.spans()

    assert not result.timed_out, result.stderr.decode(errors="replace")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert fake_firecrawl.calls == [("POST", "/v2/scrape")]
    assert [span["name"] for span in spans] == (["firecrawl.scrape"] if exported else [])
