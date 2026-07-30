"""Tests for LangGraph instrumentor module."""


class TestLangGraphInstrumentor:
    """Test LangGraphInstrumentor class."""

    def test_import(self):
        """Test that instrumentor can be imported."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor
        assert LangGraphInstrumentor is not None

    def test_singleton_pattern(self):
        """Test singleton pattern."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        inst1 = LangGraphInstrumentor()
        inst2 = LangGraphInstrumentor()

        assert inst1 is inst2

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        instrumentor = LangGraphInstrumentor()
        deps = instrumentor.instrumentation_dependencies()

        assert len(deps) > 0
        assert any("langgraph" in dep for dep in deps)

    def test_is_instrumented_property(self):
        """Test is_instrumented property."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        instrumentor = LangGraphInstrumentor()
        assert instrumentor.is_instrumented is False

    def test_graph_wrapper_property_before_instrument(self):
        """Test graph_wrapper property before instrumentation."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        instrumentor = LangGraphInstrumentor()
        assert instrumentor.graph_wrapper is None

    def test_state_tracker_property_before_instrument(self):
        """Test state_tracker property before instrumentation."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        instrumentor = LangGraphInstrumentor()
        assert instrumentor.state_tracker is None

    def test_get_topology_before_instrument(self):
        """Test get_topology before instrumentation."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        instrumentor = LangGraphInstrumentor()
        assert instrumentor.get_topology() is None

    def test_get_state_history_before_instrument(self):
        """Test get_state_history before instrumentation."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        instrumentor = LangGraphInstrumentor()
        history = instrumentor.get_state_history()
        assert history == []

    def test_get_memory_stats_before_instrument(self):
        """Test get_memory_stats before instrumentation."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        instrumentor = LangGraphInstrumentor()
        stats = instrumentor.get_memory_stats()
        assert stats == {}

    # --- Shim contract -----------------------------------------------------
    # LangGraphInstrumentor is now a deprecated no-op: node/tool/LLM spans are
    # captured automatically by LangChainInstrumentor's callback handler. These
    # tests assert the instrumentor NEVER patches LangGraph (which used to break
    # async nodes and corrupt concurrent state).

    def _fresh(self):
        from opentelemetry.sdk.trace import TracerProvider
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False
        return LangGraphInstrumentor(), TracerProvider()

    def test_instrument_does_not_patch_stategraph(self):
        """The shim must leave StateGraph.add_node / compile untouched."""
        from langgraph.graph.state import StateGraph

        orig_add_node = StateGraph.add_node
        orig_compile = StateGraph.compile

        inst, provider = self._fresh()
        inst.instrument(tracer_provider=provider)
        try:
            assert StateGraph.add_node is orig_add_node
            assert StateGraph.compile is orig_compile
        finally:
            inst.uninstrument()

    def test_instrument_toggles_is_instrumented(self):
        inst, provider = self._fresh()
        inst.instrument(tracer_provider=provider)
        assert inst.is_instrumented is True
        inst.uninstrument()
        assert inst.is_instrumented is False

    def test_double_instrument_is_safe_and_leaves_stategraph_unpatched(self):
        """A second instantiation + instrument() must not break StateGraph
        (the old singleton __init__-rerun nulled saved originals — finding #7)."""
        from langgraph.graph.state import StateGraph
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        orig_add_node = StateGraph.add_node
        inst, provider = self._fresh()
        inst.instrument(tracer_provider=provider)
        LangGraphInstrumentor().instrument(tracer_provider=provider)  # 2nd call
        try:
            assert StateGraph.add_node is orig_add_node
            # A graph can still be built and add_node still works.
            g = StateGraph(dict)
            g.add_node("n", lambda s: s)
        finally:
            inst.uninstrument()

    def test_instrument_logs_deprecation_warning(self, caplog):
        import logging
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        inst, provider = self._fresh()
        LangGraphInstrumentor._deprecation_logged = False
        with caplog.at_level(logging.WARNING):
            inst.instrument(tracer_provider=provider)
        inst.uninstrument()
        assert any("deprecated" in r.message.lower() for r in caplog.records)

    def test_uninstrument_is_idempotent(self):
        inst, provider = self._fresh()
        inst.instrument(tracer_provider=provider)
        inst.uninstrument()
        inst.uninstrument()  # second call must be a safe no-op
        assert inst.is_instrumented is False


class TestLangGraphInstrumentorExports:
    """Test LangGraph instrumentor exports."""

    def test_main_init_exports(self):
        """Test that main __init__ exports LangGraph classes."""
        from traceai_langchain import LangGraphInstrumentor, LangGraphAttributes

        assert LangGraphInstrumentor is not None
        assert LangGraphAttributes is not None

    def test_langgraph_submodule_exports(self):
        """Test that _langgraph submodule exports correctly."""
        from traceai_langchain._langgraph import (
            LangGraphInstrumentor,
            LangGraphAttributes,
        )

        assert LangGraphInstrumentor is not None
        assert LangGraphAttributes is not None
