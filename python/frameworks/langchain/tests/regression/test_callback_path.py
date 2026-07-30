"""Behaviours the callback path (LangChainInstrumentor alone) must deliver.

This is the supported / post-fix setup. Tests here assert the observable span
behaviour the customer needs. Some are GREEN today (characterisation guards that
lock in working behaviour); two are RED today and flag audit findings that the
callback-first fix must deliver (graph-node enrichment, and interrupts not being
errors). Each RED test names the finding it flags and why it currently fails.

Status legend in docstrings:
  GREEN  — passes today; guards existing correct behaviour against regression.
  RED    — fails today; flags an audit finding; must pass after the fix.
"""
from __future__ import annotations

import asyncio

import pytest

from ._helpers import (
    GRAPH_NODE_NAME_ATTR,
    INPUT,
    SPAN_KIND_ATTR,
    build_async_graph,
    build_async_interrupt_graph,
    build_barrier_async_graph,
    build_error_graph,
    build_interrupt_graph,
    build_llm_node_graph,
    build_sync_graph,
    build_tool_graph,
    span_names,
    spans_named,
)


def test_sync_node_emits_span_named_by_node(lc_tracing):
    """GREEN. Each graph node becomes a LangChain chain run named by the node,
    so a span literally named 'agent' is emitted — no LangGraph instrumentor needed."""
    app = build_sync_graph()
    app.invoke(INPUT)
    assert "agent" in span_names(lc_tracing)


@pytest.mark.asyncio
async def test_async_node_traces_without_crash(lc_tracing):
    """GREEN. The callback path never wraps/invokes the node itself, so an async
    node runs normally and still produces a node span. (Contrast: finding #1, where
    the LangGraph instrumentor's sync wrapper crashes on the same async node.)"""
    app = build_async_graph()
    out = await app.ainvoke(INPUT)
    assert out["value"] == 2
    assert "agent" in span_names(lc_tracing)


def test_tool_node_emits_tool_span(lc_tracing):
    """GREEN. A ToolNode's tool executions surface as tool-kind spans through
    callbacks — so tool tracing works on the callback path even though the
    LangGraph instrumentor never wraps a ToolNode (finding #8)."""
    app = build_tool_graph()
    app.invoke(INPUT)
    kinds = [(s.attributes or {}).get(SPAN_KIND_ATTR) for s in lc_tracing.get_finished_spans()]
    assert "TOOL" in kinds, f"expected a TOOL span, got kinds={kinds}"


def test_graph_node_enrichment_attribute_present(lc_tracing):
    """RED — flags Area 1 (missing graph enrichment).

    LangGraph puts `langgraph_node` in each node run's callback metadata, but the
    tracer does not yet map it onto the canonical `gen_ai.agent.graph.node_name`
    attribute. Fails today (attribute absent); passes once the enrichment mapping
    is added to `_tracer._metadata`."""
    app = build_sync_graph()
    app.invoke(INPUT)
    node_spans = spans_named(lc_tracing, "agent")
    assert node_spans, "no node span named 'agent'"
    assert any(
        (s.attributes or {}).get(GRAPH_NODE_NAME_ATTR) == "agent" for s in node_spans
    ), f"{GRAPH_NODE_NAME_ATTR} not set on the node span"


def test_interrupt_node_span_is_not_error(lc_tracing):
    """RED — flags finding #4 (HITL interrupt traced as an error).

    A dynamic `interrupt()` raises GraphInterrupt, which reaches the tracer's
    `on_chain_error` and marks the node span ERROR with a recorded exception.
    Fails today (status ERROR); passes once GraphBubbleUp is treated as an
    intentional, non-error interrupt (Area 2)."""
    app = build_interrupt_graph()
    app.invoke(INPUT, {"configurable": {"thread_id": "t-interrupt"}})

    node_spans = spans_named(lc_tracing, "ask_human")
    assert node_spans, "no node span named 'ask_human'"
    for s in node_spans:
        assert s.status.status_code.name != "ERROR", (
            "interrupt marked the node span as ERROR "
            f"(events={[e.name for e in s.events]})"
        )


def test_session_id_propagates_from_using_session(lc_tracing):
    """GREEN. Binding session_id = thread_id via `using_session` (the customer's
    session-grouping approach) lands `session.id` on every span."""
    from fi_instrumentation.instrumentation.context_attributes import using_session

    app = build_sync_graph()
    with using_session("thread-xyz"):
        app.invoke(INPUT)

    spans = lc_tracing.get_finished_spans()
    assert spans, "no spans exported"
    assert any((s.attributes or {}).get("session.id") == "thread-xyz" for s in spans), (
        "session.id not propagated to any span"
    )


def test_real_error_still_marks_span_error(lc_tracing):
    """GREEN guard (closes a coverage gap). A genuine exception in a node must
    still produce an ERROR span with a recorded exception — the interrupt handling
    (Area 2) must NOT suppress real failures. Guards against a future broadening of
    `_is_graph_interrupt` that would silently swallow node errors."""
    app = build_error_graph()
    with pytest.raises(ValueError):
        app.invoke(INPUT)
    node_spans = spans_named(lc_tracing, "boom")
    assert node_spans, "no node span named 'boom'"
    assert all(s.status.status_code.name == "ERROR" for s in node_spans), (
        "a real node error was not recorded as ERROR"
    )
    assert all(any(e.name == "exception" for e in s.events) for s in node_spans), (
        "no exception event recorded on the errored node span"
    )


@pytest.mark.asyncio
async def test_async_interrupt_node_span_is_not_error(lc_tracing):
    """GREEN guard for the customer's ASYNC HITL path. An async node that calls
    interrupt() must yield an OK node span with an interrupt marker (not ERROR)."""
    app = build_async_interrupt_graph()
    await app.ainvoke(INPUT, {"configurable": {"thread_id": "async-hitl"}})
    node_spans = spans_named(lc_tracing, "ask_human")
    assert node_spans, "no async node span named 'ask_human'"
    for s in node_spans:
        assert s.status.status_code.name != "ERROR", "async interrupt marked node ERROR"
        assert (s.attributes or {}).get("langgraph.interrupt") is True


def test_node_enrichment_not_on_child_llm_span(lc_tracing):
    """The canonical graph-node attribute belongs on the node's own span, not on
    nested LLM/tool child spans (which inherit `langgraph_node` metadata) — else a
    consumer counting node-id spans over-counts."""
    app = build_llm_node_graph()
    app.invoke(INPUT)
    spans = lc_tracing.get_finished_spans()
    node = [s for s in spans if s.name == "agent"]
    llm = [s for s in spans if (s.attributes or {}).get(SPAN_KIND_ATTR) == "LLM"]
    assert node and llm, f"need both node and LLM spans; got {[s.name for s in spans]}"
    assert all((s.attributes or {}).get(GRAPH_NODE_NAME_ATTR) == "agent" for s in node)
    assert all(GRAPH_NODE_NAME_ATTR not in (s.attributes or {}) for s in llm), (
        "canonical node-name attribute leaked onto a child LLM span"
    )


@pytest.mark.asyncio
async def test_concurrent_ainvoke_isolated_span_trees(lc_tracing):
    """GREEN. Two concurrent conversations, forced to interleave, produce two
    disjoint trace trees — the base tracer keys everything by run.id, so there is
    no shared-state cross-contamination. (Contrast: finding #2 on the LangGraph
    instrumentor's shared singleton.)"""
    enter_a, release_a = asyncio.Event(), asyncio.Event()
    enter_b, release_b = asyncio.Event(), asyncio.Event()
    app_a = build_barrier_async_graph(enter_a, release_a)
    app_b = build_barrier_async_graph(enter_b, release_b)

    async def run(app, tid):
        return await app.ainvoke(INPUT, {"configurable": {"thread_id": tid}})

    task_a = asyncio.create_task(run(app_a, "A"))
    task_b = asyncio.create_task(run(app_b, "B"))
    # Force interleave: both graphs are parked inside their node simultaneously.
    await asyncio.wait_for(enter_a.wait(), timeout=5)
    await asyncio.wait_for(enter_b.wait(), timeout=5)
    release_a.set()
    release_b.set()
    await asyncio.gather(task_a, task_b)

    trace_ids = {s.context.trace_id for s in lc_tracing.get_finished_spans()}
    assert len(trace_ids) == 2, f"expected 2 isolated traces, got {len(trace_ids)}"
