"""Shared-harness contract test for traceAI-parallel (TH-8326, harness Receiver).

The real ``parallel-web`` client makes real HTTP calls to the loopback fake of
the Parallel API. Spans leave through the real ``fi_instrumentation.register()``
OTLP exporter into ``harness.Receiver``. Nothing calls api.parallel.ai; every
key is a placeholder.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes  # noqa: E402

from _parallel_support import (  # noqa: E402
    CONTENT_MARKERS,
    EXCERPT,
    EXTRACT_ID,
    FAIL_401,
    PARALLEL_KEY,
    SEARCH_ID,
    FakeParallel,
    async_client,
    sync_client,
)

FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
PROJECT = "parallel-contract"
SECRET_URL = "https://x.example/page?token=URL-SECRET"
OBJECTIVE = "OBJECTIVE-TEXT-OFF-BY-DEFAULT"


def _journey(receiver, fake, monkeypatch, **options) -> List[dict]:
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    from traceai_parallel import ParallelInstrumentor

    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
    provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)
    instrumentor = ParallelInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        client = sync_client(fake)
        found = client.search(search_queries=["open telemetry", "otlp"], mode="turbo", objective=OBJECTIVE)
        # The content flowed into the client, so its absence below is meaningful.
        assert found.results[0].excerpts == [EXCERPT]
        client.extract(urls=[SECRET_URL, "https://x.example/missing"], objective=OBJECTIVE)
        with pytest.raises(Exception):
            client.search(search_queries=[FAIL_401])

        async def call() -> None:
            async_parallel = async_client(fake)
            try:
                await async_parallel.search(search_queries=["async search"])
            finally:
                await async_parallel.close()

        asyncio.run(call())
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    return receiver.spans()


def _by_name(spans) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for span in spans:
        grouped.setdefault(span["name"], []).append(span)
    return grouped


def test_real_client_calls_reach_the_collector_contract(monkeypatch):
    with FakeParallel() as fake, Receiver() as receiver:
        spans = _journey(receiver, fake, monkeypatch)
        exports = receiver.requests()

    # The real SDK made every HTTP call (no monkeypatched client methods).
    assert fake.paths() == ["/v1/search", "/v1/extract", "/v1/search", "/v1/search"]

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

    by_name = _by_name(spans)
    assert sorted((name, len(group)) for name, group in by_name.items()) == [
        ("parallel.extract", 1),
        ("parallel.search", 3),
    ]
    ok_search, failed_search, async_search = by_name["parallel.search"]

    search = _flatten_attributes(ok_search["attributes"])
    assert search["fi.span.kind"] == "RETRIEVER"
    assert search["parallel.mode"] == "turbo"
    assert int(search["parallel.query_count"]) == 2
    assert int(search["parallel.result_count"]) == 2
    assert search["parallel.search_id"] == SEARCH_ID
    assert search["input.value"] == "open telemetry\notlp"
    assert search["gen_ai.retrieval.query"] == "open telemetry\notlp"
    # register()'s processor also promotes UNSET to OK on export
    # (fi_instrumentation/otel.py _auto_set_ok_status), so the wrapper's own
    # OK is pinned by the in-memory tests.
    assert ok_search["status"]["code"] == "STATUS_CODE_OK"
    assert not any("model" in key or "token" in key or "cost" in key for key in search)

    extract = _flatten_attributes(by_name["parallel.extract"][0]["attributes"])
    assert extract["fi.span.kind"] == "RETRIEVER"
    assert int(extract["parallel.url_count"]) == 2
    assert int(extract["parallel.result_count"]) == 1
    assert int(extract["parallel.failed_url_count"]) == 1
    assert extract["parallel.extract_id"] == EXTRACT_ID
    assert "input.value" not in extract
    assert by_name["parallel.extract"][0]["status"]["code"] == "STATUS_CODE_OK"

    failed = _flatten_attributes(failed_search["attributes"])
    assert failed["input.value"] == FAIL_401
    assert failed_search["status"]["code"] == "STATUS_CODE_ERROR"
    assert any(event["name"] == "exception" for event in failed_search.get("events", []))

    assert _flatten_attributes(async_search["attributes"])["input.value"] == "async search"
    assert async_search["status"]["code"] == "STATUS_CODE_OK"


def test_no_key_or_content_is_exported(monkeypatch):
    with FakeParallel() as fake, Receiver() as receiver:
        spans = _journey(receiver, fake, monkeypatch)
        exports = receiver.requests()

    wire = json.dumps(spans) + json.dumps(exports)
    # The 401 body echoed the key into the vendor's error message.
    for secret in (PARALLEL_KEY, OBJECTIVE, "URL-SECRET", "x.example") + CONTENT_MARKERS:
        assert secret not in wire, secret


def test_opt_in_capture_reaches_the_collector(monkeypatch):
    # Control for the test above: the same journey with capture on carries
    # the objective and URLs on the wire, still without the key.
    with FakeParallel() as fake, Receiver() as receiver:
        spans = _journey(receiver, fake, monkeypatch, capture_urls=True, capture_objective=True)

    wire = json.dumps(spans)
    assert OBJECTIVE in wire
    assert "URL-SECRET" in wire
    assert PARALLEL_KEY not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker
