"""Source-only private-state regression; still required by ordinary package CI."""
from ._helpers import build_interrupt_graph, INPUT

def test_interrupt_bookkeeping_is_drained(lc_tracing):
    """The tracer's interrupt bookkeeping must not leak — including when instrumentation
    is suppressed only at end time (a run started unsuppressed, ended suppressed)."""
    from fi_instrumentation import suppress_tracing
    from traceai_langchain import LangChainInstrumentor

    tracer = LangChainInstrumentor()._tracer
    app = build_interrupt_graph()
    app.invoke(INPUT, {"configurable": {"thread_id": "drain-normal"}})
    assert len(tracer._interrupt_run_ids) == 0, "interrupt ids leaked on a normal turn"
    with suppress_tracing():
        app.invoke(INPUT, {"configurable": {"thread_id": "drain-suppressed"}})
    assert len(tracer._interrupt_run_ids) == 0, "interrupt ids leaked under suppression"
