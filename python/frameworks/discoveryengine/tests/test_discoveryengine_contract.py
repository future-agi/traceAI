"""Shared-harness contract tests for traceAI-discoveryengine (TH-8332).

The real ``google-cloud-discoveryengine`` clients make real gRPC calls to the
loopback fake. Spans leave through the real ``fi_instrumentation.register()``
OTLP exporter into ``harness.Receiver``. The architecture's round-trip check,
``harness.post_otlp()`` of spans the unit tests captured in memory, is the
last test. Nothing calls discoveryengine.googleapis.com, no ADC is read and
every credential is a placeholder.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List

import pytest

pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes, post_otlp  # noqa: E402

from _discoveryengine_support import (  # noqa: E402
    ACCESS_TOKEN,
    ANSWER_REFERENCES,
    ANSWER_TEXT,
    CONTENT_MARKERS,
    FAIL_DENIED,
    SEARCH_RESULTS,
    SERVING_CONFIG,
    SESSION_NAME,
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    async_search_client,
    instrumented,
    oauth_credentials,
    search_client,
    search_request,
)

FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
PROJECT = "discoveryengine-contract"
QUERY = "CONTRACT-QUERY-OFF-BY-DEFAULT"


def _journey(receiver, fake, monkeypatch, **options) -> List[dict]:
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    from traceai_discoveryengine import DiscoveryEngineInstrumentor

    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
    provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)
    instrumentor = DiscoveryEngineInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        client = search_client(fake, credentials=oauth_credentials())
        with provider.get_tracer("app").start_as_current_span("app.request"):
            pager = client.search(request=search_request(QUERY))
            # The content flowed into the client, so its absence below is meaningful.
            assert len(pager.results) == SEARCH_RESULTS
            client.search_lite(request=search_request(QUERY))
            response = answer_client(fake).answer_query(
                request=answer_request(QUERY, session=SESSION_NAME)
            )
            assert response.answer.answer_text == ANSWER_TEXT
        with pytest.raises(Exception):
            client.search(request=search_request(FAIL_DENIED + " " + QUERY))

        async def call() -> None:
            async_client = async_search_client(fake)
            try:
                await async_client.search(request=search_request(QUERY))
            finally:
                await async_client.transport.close()

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
    with FakeDiscoveryEngine() as fake, Receiver() as receiver:
        spans = _journey(receiver, fake, monkeypatch)
        exports = receiver.requests()

    # The real clients made every RPC (no monkeypatched client methods).
    assert fake.methods() == ["Search", "SearchLite", "AnswerQuery", "Search", "Search"]

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
        ("app.request", 1),
        ("discoveryengine.answer_query", 1),
        ("discoveryengine.search", 3),
        ("discoveryengine.search_lite", 1),
    ]
    (parent,) = by_name["app.request"]
    ok_search, failed_search, async_search = by_name["discoveryengine.search"]
    (lite,) = by_name["discoveryengine.search_lite"]
    (answer,) = by_name["discoveryengine.answer_query"]

    # Children of the active span, in its trace; the later calls are roots.
    for child in (ok_search, lite, answer):
        assert child["parentSpanId"] == parent["spanId"]
        assert child["traceId"] == parent["traceId"]
    assert not failed_search.get("parentSpanId")
    assert not async_search.get("parentSpanId")

    for span in (ok_search, lite, async_search):
        values = _flatten_attributes(span["attributes"])
        assert values["fi.span.kind"] == "RETRIEVER"
        assert values["discoveryengine.serving_config"] == SERVING_CONFIG
        assert int(values["discoveryengine.result_count"]) == SEARCH_RESULTS
        assert "input.value" not in values
        # register()'s processor also promotes UNSET to OK on export
        # (fi_instrumentation/otel.py _auto_set_ok_status), so the wrapper's
        # own OK is pinned by the in-memory tests.
        assert span["status"]["code"] == "STATUS_CODE_OK"

    values = _flatten_attributes(answer["attributes"])
    assert values["fi.span.kind"] == "RETRIEVER"
    assert int(values["discoveryengine.result_count"]) == ANSWER_REFERENCES
    assert int(values["discoveryengine.answer.length"]) == len(ANSWER_TEXT)
    assert values["discoveryengine.answer.state"] == "SUCCEEDED"
    assert values["discoveryengine.session"] == SESSION_NAME
    assert not any("model" in key or "token" in key or "cost" in key for key in values)

    failed = _flatten_attributes(failed_search["attributes"])
    assert failed["discoveryengine.error.status"] == "PERMISSION_DENIED"
    assert int(failed["discoveryengine.error.code"]) == 403
    assert "discoveryengine.result_count" not in failed
    assert failed_search["status"]["code"] == "STATUS_CODE_ERROR"
    assert [event["name"] for event in failed_search.get("events", [])] == ["exception"]


def test_no_credential_query_or_content_is_exported(monkeypatch):
    with FakeDiscoveryEngine() as fake, Receiver() as receiver:
        spans = _journey(receiver, fake, monkeypatch)
        exports = receiver.requests()

    # The fake received the token, and its PERMISSION_DENIED message echoed
    # both the token and the query into the vendor's exception.
    assert fake.calls[0].metadata["authorization"] == "Bearer " + ACCESS_TOKEN
    wire = json.dumps(spans) + json.dumps(exports)
    for secret in (ACCESS_TOKEN, QUERY) + CONTENT_MARKERS:
        assert secret not in wire, secret


def test_capture_query_reaches_the_collector(monkeypatch):
    # Control for the test above: with capture on, the query is on the wire,
    # still without the token or any response content.
    with FakeDiscoveryEngine() as fake, Receiver() as receiver:
        spans = _journey(receiver, fake, monkeypatch, capture_query=True)

    search = _flatten_attributes(_by_name(spans)["discoveryengine.search"][0]["attributes"])
    assert search["input.value"] == QUERY
    assert search["gen_ai.retrieval.query"] == QUERY
    wire = json.dumps(spans)
    assert ACCESS_TOKEN not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker


def test_post_otlp_round_trip_of_captured_spans():
    # The architecture's harness use: post_otlp() of spans the unit test
    # captured with InMemorySpanExporter, no child process.
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    with FakeDiscoveryEngine() as fake:
        with instrumented() as traced:
            search_client(fake).search(request=search_request())
            answer_client(fake).answer_query(request=answer_request())
    captured = traced.spans()
    payload = MessageToDict(encode_spans(captured))

    with Receiver() as receiver:
        assert post_otlp(payload, receiver.endpoint) == 200
        received = receiver.spans()

    assert [span["name"] for span in received] == [span.name for span in captured]
    for sent, got in zip(captured, received):
        values = _flatten_attributes(got["attributes"])
        assert set(values) == set(sent.attributes)
        for key, value in sent.attributes.items():
            assert str(values[key]) == str(value), key
