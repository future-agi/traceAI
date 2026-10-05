"""``session.id`` from LangGraph's ``configurable.thread_id``.

LangGraph copies ``configurable.thread_id`` into run metadata, so every node,
LLM and tool run of a thread carries it. traceai-langchain already emitted it as
``thread_id`` and ``gen_ai.conversation.id`` but not as ``session.id``, so a
LangGraph or Deep Agents run passing ``config={"configurable": {"thread_id": ...}}``
had no session in Observe unless the app also wrapped it in ``using_session``.

Precedence: ``metadata={"session_id": ...}`` > ``using_session(...)`` > ``thread_id``.
The first two predate this change (``_metadata`` overwrites the captured context).
"""

from __future__ import annotations

import contextlib

import pytest
from langchain_core.runnables import RunnableLambda
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def exporter():
    from traceai_langchain import LangChainInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter
    finally:
        with contextlib.suppress(Exception):
            instrumentor.uninstrument()


def _sessions(exporter):
    spans = exporter.get_finished_spans()
    assert spans, "no spans exported"
    return [(span.attributes or {}).get("session.id") for span in spans]


def test_thread_id_in_metadata_becomes_session_id(exporter):
    RunnableLambda(lambda x: x).invoke(1, {"metadata": {"thread_id": "t-9"}})
    assert _sessions(exporter) == ["t-9"]


def test_thread_id_from_langgraph_configurable_becomes_session_id(exporter):
    langgraph = pytest.importorskip("langgraph.graph")
    from typing import TypedDict

    class State(TypedDict):
        n: int

    graph = langgraph.StateGraph(State)
    graph.add_node("step", lambda state: {"n": state["n"] + 1})
    graph.add_edge(langgraph.START, "step")
    graph.add_edge("step", langgraph.END)
    graph.compile().invoke({"n": 0}, {"configurable": {"thread_id": "t-graph"}})

    sessions = _sessions(exporter)
    assert len(sessions) >= 2  # graph root + node
    assert set(sessions) == {"t-graph"}


def test_non_string_thread_id_is_stringified(exporter):
    RunnableLambda(lambda x: x).invoke(1, {"metadata": {"thread_id": 42}})
    assert _sessions(exporter) == ["42"]


def test_metadata_session_id_wins_over_thread_id(exporter):
    RunnableLambda(lambda x: x).invoke(
        1, {"metadata": {"thread_id": "t-9", "session_id": "explicit"}}
    )
    assert _sessions(exporter) == ["explicit"]


def test_using_session_wins_over_thread_id(exporter):
    from fi_instrumentation.instrumentation.context_attributes import using_session

    with using_session("from-context"):
        RunnableLambda(lambda x: x).invoke(1, {"metadata": {"thread_id": "t-9"}})
    assert _sessions(exporter) == ["from-context"]


def test_metadata_session_id_wins_over_using_session(exporter):
    from fi_instrumentation.instrumentation.context_attributes import using_session

    with using_session("from-context"):
        RunnableLambda(lambda x: x).invoke(1, {"metadata": {"thread_id": "t-9", "session_id": "explicit"}})
    assert _sessions(exporter) == ["explicit"]


@pytest.mark.parametrize("empty", ["", "   "])
def test_blank_thread_id_sets_no_session_id(exporter, empty):
    RunnableLambda(lambda x: x).invoke(1, {"metadata": {"thread_id": empty}})
    assert _sessions(exporter) == [None]


def test_session_set_inside_a_node_only_covers_that_node(exporter):
    """Documented limit: the fallback is decided per run. A session set for only part of a
    graph run (here, using_session inside a node) applies to the runs inside it; the outer
    graph and node runs fall back to thread_id. Wrap the whole invoke to get one session."""
    langgraph = pytest.importorskip("langgraph.graph")
    from typing import TypedDict

    from fi_instrumentation.instrumentation.context_attributes import using_session

    class State(TypedDict):
        n: int

    inner = RunnableLambda(lambda x: x + 1).with_config(run_name="inner")

    def step(state):
        with using_session("narrow"):
            return {"n": inner.invoke(state["n"])}

    graph = langgraph.StateGraph(State)
    graph.add_node("step", step)
    graph.add_edge(langgraph.START, "step")
    graph.add_edge("step", langgraph.END)
    graph.compile().invoke({"n": 0}, {"configurable": {"thread_id": "t-outer"}})

    by_name = {span.name: (span.attributes or {}).get("session.id") for span in exporter.get_finished_spans()}
    assert by_name["inner"] == "narrow"
    assert by_name["step"] == "t-outer"


def test_no_thread_id_no_session_id(exporter):
    RunnableLambda(lambda x: x).invoke(1, {"metadata": {"other": "x"}})
    assert _sessions(exporter) == [None]
