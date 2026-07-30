"""Fixtures for the LangGraph tracing regression suite.

Two tracing setups, matching the two layers under test:

* ``lc_tracing``   — ONLY the callback-based ``LangChainInstrumentor``. This is the
  supported / post-fix path. Node, LLM and tool spans come from LangChain callbacks,
  isolated per ``run.id``.
* ``both_tracing`` — ``LangChainInstrumentor`` **and** the monkey-patch
  ``LangGraphInstrumentor`` (the customer's real setup, per handoff §4). This is where
  the node-wrapping defects live.

Both fixtures pass ``tracer_provider`` explicitly to ``.instrument()`` and never touch
the global provider, so each test gets an isolated in-memory exporter with no
cross-test contamination.
"""
from __future__ import annotations

import contextlib

import pytest
from langgraph.graph.state import StateGraph
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Captured at import, before any instrumentation runs — the pristine unpatched
# StateGraph methods. The monkey-patch LangGraphInstrumentor mutates these globally
# and (finding #7) can fail to restore them, so we force-restore after every test to
# keep tests hermetic and order-independent.
_PRISTINE_ADD_NODE = StateGraph.add_node
_PRISTINE_COMPILE = StateGraph.compile


def _make_provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


def _reset_langgraph_singleton():
    from traceai_langchain import LangGraphInstrumentor

    LangGraphInstrumentor._instance = None
    LangGraphInstrumentor._is_instrumented = False


@pytest.fixture
def lc_tracing():
    """Enable only the callback-based LangChainInstrumentor. Yields the exporter."""
    from traceai_langchain import LangChainInstrumentor

    exporter, provider = _make_provider()
    inst = LangChainInstrumentor()
    inst.instrument(tracer_provider=provider)
    try:
        yield exporter
    finally:
        with contextlib.suppress(Exception):
            inst.uninstrument()


@pytest.fixture
def both_tracing():
    """Enable LangChain + LangGraph instrumentors (customer setup). Yields the exporter."""
    from traceai_langchain import LangChainInstrumentor, LangGraphInstrumentor

    exporter, provider = _make_provider()
    lc = LangChainInstrumentor()
    lc.instrument(tracer_provider=provider)
    _reset_langgraph_singleton()
    lg = LangGraphInstrumentor()
    lg.instrument(tracer_provider=provider)
    try:
        yield exporter
    finally:
        with contextlib.suppress(Exception):
            lg.uninstrument()
        with contextlib.suppress(Exception):
            lc.uninstrument()


@pytest.fixture(autouse=True)
def _hermetic_stategraph():
    """Force-restore pristine StateGraph methods and reset the LangGraph singleton
    after every test, so a leaked global patch can't corrupt an unrelated test."""
    yield
    StateGraph.add_node = _PRISTINE_ADD_NODE
    StateGraph.compile = _PRISTINE_COMPILE
    _reset_langgraph_singleton()


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: heavy tests (real dependency resolution) — opt-in")
