"""Collector contract through the shared harness (TH-8339 Receiver).

The real tavily-python clients make real HTTP calls to the loopback fake;
spans leave through the real ``fi_instrumentation.register()`` OTLP exporter
into ``harness.Receiver``. Nothing calls api.tavily.com; every key is a
placeholder.

register()'s processor turns UNSET into OK before export, so a wire-level OK
does not prove the wrapper set it; test_spans.py asserts the wrapper's own OK
on an in-memory provider.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Tuple

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to test its instrumentor")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

from _tavily_fake import CONTENT_MARKERS, FAIL_401, TAVILY_KEY, FakeTavily  # noqa: E402
from harness import Receiver, _flatten_attributes  # noqa: E402

FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
PROJECT = "tavily-contract"
URLS = ["https://example.com/private?token=abc", "https://example.com/fail-b"]
KEYED_QUERY = "agent asked about {0}".format(TAVILY_KEY)


def _journey(
    receiver: Receiver, fake: FakeTavily, monkeypatch: pytest.MonkeyPatch
) -> Tuple[List[dict], List[dict], Dict[str, Any]]:
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from tavily import AsyncTavilyClient, InvalidAPIKeyError, TavilyClient

    from traceai_tavily import TavilyInstrumentor

    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
    provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)
    instrumentor = TavilyInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    returned: Dict[str, Any] = {}

    async def async_calls() -> None:
        client = AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)
        try:
            returned["async_search"] = await client.search("async question", max_results=1)
            returned["async_extract"] = await client.extract(URLS[:1])
        finally:
            await client.close()

    try:
        tracer = provider.get_tracer("contract")
        with tracer.start_as_current_span("agent-turn"):
            client = TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)
            returned["search"] = client.search(KEYED_QUERY, max_results=2)
            returned["extract"] = client.extract(URLS)
            with pytest.raises(InvalidAPIKeyError):
                client.search(FAIL_401)
            asyncio.run(async_calls())
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    return receiver.spans(), receiver.requests(), returned


@pytest.fixture()
def journey(monkeypatch):
    with FakeTavily() as fake, Receiver() as receiver:
        spans, exports, returned = _journey(receiver, fake, monkeypatch)
        paths = fake.paths()
    return spans, exports, returned, paths


def test_real_client_calls_reach_the_collector_contract(journey):
    spans, exports, _, paths = journey

    # The real SDK made the HTTP calls.
    assert paths == ["/search", "/extract", "/search", "/search", "/extract"]

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
        ("agent-turn", 1),
        ("tavily.extract", 2),
        ("tavily.search", 3),
    ]
    parent = by_name["agent-turn"][0]
    for name in ("tavily.search", "tavily.extract"):
        for span in by_name[name]:
            values = _flatten_attributes(span["attributes"])
            assert values["gen_ai.span.kind"] == "TOOL"
            assert values["gen_ai.tool.name"] == name
            assert span["parentSpanId"] == parent["spanId"]
            assert span["traceId"] == parent["traceId"]

    search_ok, search_failed, async_search = by_name["tavily.search"]
    ok = _flatten_attributes(search_ok["attributes"])
    assert ok["input.value"] == "agent asked about [redacted]"
    assert int(ok["tavily.result_count"]) == 2
    assert search_ok["status"]["code"] == "STATUS_CODE_OK"

    failed = _flatten_attributes(search_failed["attributes"])
    assert failed["input.value"] == FAIL_401
    assert "tavily.result_count" not in failed
    assert search_failed["status"]["code"] == "STATUS_CODE_ERROR"
    assert search_failed["status"]["message"] == (
        "InvalidAPIKeyError: Unauthorized: invalid API key."
    )
    assert [event["name"] for event in search_failed.get("events", [])] == ["exception"]

    assert int(_flatten_attributes(async_search["attributes"])["tavily.result_count"]) == 1

    extract, async_extract = by_name["tavily.extract"]
    values = _flatten_attributes(extract["attributes"])
    assert int(values["tavily.url_count"]) == 2
    assert int(values["tavily.result_count"]) == 1
    assert int(values["tavily.failed_result_count"]) == 1
    assert "input.value" not in values
    assert int(_flatten_attributes(async_extract["attributes"])["tavily.url_count"]) == 1


def test_no_key_urls_or_response_content_are_exported(journey):
    spans, exports, returned, _ = journey

    # Control: the content reached the caller, so its absence below is real.
    caller = json.dumps(returned)
    assert all(marker in caller for marker in CONTENT_MARKERS)
    assert URLS[0] in caller

    wire = json.dumps(spans) + json.dumps(exports)
    for secret in (TAVILY_KEY, "tvly-", *CONTENT_MARKERS, *URLS, "token=abc"):
        assert secret not in wire, secret
