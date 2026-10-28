"""Instrumentor lifecycle: config, context attributes, uninstrument, vendor drift."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to test its instrumentor")

from fi_instrumentation import TraceConfig, suppress_tracing, using_session, using_user  # noqa: E402
from opentelemetry import trace as trace_api  # noqa: E402

from _support import attrs, instrumented, new_provider  # noqa: E402
from _tavily_fake import TAVILY_KEY, FakeTavily  # noqa: E402

_METHODS = [
    ("tavily.tavily", "TavilyClient", "search"),
    ("tavily.tavily", "TavilyClient", "extract"),
    ("tavily.async_tavily", "AsyncTavilyClient", "search"),
    ("tavily.async_tavily", "AsyncTavilyClient", "extract"),
]


@pytest.fixture()
def fake():
    with FakeTavily() as server:
        yield server


def _class(module_name: str, class_name: str) -> Any:
    import importlib

    return getattr(importlib.import_module(module_name), class_name)


def _search(fake: FakeTavily, query: str = "q") -> Any:
    from tavily import TavilyClient

    return TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin).search(query)


async def _async_extract(fake: FakeTavily) -> Any:
    from tavily import AsyncTavilyClient

    client = AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)
    try:
        return await client.extract(["https://example.com/a"])
    finally:
        await client.close()


def test_uninstrument_restores_every_method_and_stops_tracing(fake):
    from traceai_tavily import TavilyInstrumentor

    originals = {
        (m, c, name): vars(_class(m, c))[name] for m, c, name in _METHODS
    }
    traced = new_provider()
    instrumentor = TavilyInstrumentor()
    instrumentor.instrument(tracer_provider=traced.provider)
    for (m, c, name), original in originals.items():
        assert vars(_class(m, c))[name] is not original, (c, name)
    instrumentor.uninstrument()

    for (m, c, name), original in originals.items():
        assert vars(_class(m, c))[name] is original, (c, name)
    _search(fake)
    asyncio.run(_async_extract(fake))
    assert traced.spans() == []


def test_instrument_again_after_uninstrument(fake):
    with instrumented():
        pass
    with instrumented() as traced:
        _search(fake)
    assert [span.name for span in traced.spans()] == ["tavily.search"]


def test_hide_inputs_config_masks_the_query(fake):
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        _search(fake, "private question")

    values = attrs(traced.one())
    assert values["input.value"] == "__REDACTED__"
    assert values["tavily.result_count"] == 2
    assert "private question" not in traced.wire()


def test_fi_hide_inputs_environment_variable_masks_the_query(fake, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    with instrumented() as traced:
        _search(fake, "private question")

    assert attrs(traced.one())["input.value"] == "__REDACTED__"


def test_config_must_be_a_trace_config():
    from tavily import TavilyClient

    from traceai_tavily import TavilyInstrumentor

    original = vars(TavilyClient)["search"]
    instrumentor = TavilyInstrumentor()
    try:
        with pytest.raises(TypeError, match="TraceConfig"):
            instrumentor.instrument(config={"hide_inputs": True})
        assert vars(TavilyClient)["search"] is original
    finally:
        instrumentor.uninstrument()
    assert vars(TavilyClient)["search"] is original


def test_session_and_user_context_reach_the_span(fake):
    with instrumented() as traced:
        with using_session("session-1"), using_user("user-1"):
            _search(fake)

    values = attrs(traced.one())
    assert values["session.id"] == "session-1"
    assert values["user.id"] == "user-1"


def test_suppress_tracing_records_nothing(fake):
    with instrumented() as traced:
        with suppress_tracing():
            result = _search(fake)

    assert len(result["results"]) == 2
    assert traced.spans() == []


def test_a_no_op_tracer_provider_is_accepted(fake):
    from traceai_tavily import TavilyInstrumentor

    instrumentor = TavilyInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.NoOpTracerProvider())
    try:
        assert len(_search(fake)["results"]) == 2
    finally:
        instrumentor.uninstrument()


def test_a_missing_vendor_class_is_skipped_with_a_warning(fake, monkeypatch, caplog):
    import tavily.async_tavily

    monkeypatch.delattr(tavily.async_tavily, "AsyncTavilyClient")
    caplog.set_level(logging.WARNING, logger="traceai_tavily")
    with instrumented() as traced:
        _search(fake)

    assert [span.name for span in traced.spans()] == ["tavily.search"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("tavily.async_tavily.AsyncTavilyClient" in message for message in warnings)


def test_a_missing_vendor_method_is_skipped_with_a_warning(fake, monkeypatch, caplog):
    from tavily import TavilyClient

    original_search = vars(TavilyClient)["search"]
    monkeypatch.delattr(TavilyClient, "extract")
    caplog.set_level(logging.WARNING, logger="traceai_tavily")
    with instrumented() as traced:
        _search(fake)

    assert [span.name for span in traced.spans()] == ["tavily.search"]
    assert vars(TavilyClient)["search"] is original_search
    assert "extract" not in vars(TavilyClient)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("TavilyClient.extract" in message for message in warnings)
