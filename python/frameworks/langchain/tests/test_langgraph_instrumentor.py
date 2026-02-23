"""Tests for LangGraph instrumentor module."""

import pytest
from unittest.mock import MagicMock, patch


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

    @patch("traceai_langchain._langgraph._instrumentor.trace_api")
    def test_instrument_creates_tracer(self, mock_trace_api):
        """Test that instrument creates a tracer."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        mock_tracer = MagicMock()
        mock_trace_api.get_tracer.return_value = mock_tracer
        mock_trace_api.get_tracer_provider.return_value = MagicMock()

        instrumentor = LangGraphInstrumentor()

        # Patch the langgraph import to avoid ImportError
        with patch.object(instrumentor, '_patch_langgraph'):
            instrumentor._instrument()

        mock_trace_api.get_tracer.assert_called()
        assert instrumentor._tracer == mock_tracer

    @patch("traceai_langchain._langgraph._instrumentor.trace_api")
    def test_instrument_creates_state_tracker(self, mock_trace_api):
        """Test that instrument creates a state tracker."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        mock_tracer = MagicMock()
        mock_trace_api.get_tracer.return_value = mock_tracer
        mock_trace_api.get_tracer_provider.return_value = MagicMock()

        instrumentor = LangGraphInstrumentor()

        with patch.object(instrumentor, '_patch_langgraph'):
            instrumentor._instrument()

        assert instrumentor.state_tracker is not None

    @patch("traceai_langchain._langgraph._instrumentor.trace_api")
    def test_instrument_creates_graph_wrapper(self, mock_trace_api):
        """Test that instrument creates a graph wrapper."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        mock_tracer = MagicMock()
        mock_trace_api.get_tracer.return_value = mock_tracer
        mock_trace_api.get_tracer_provider.return_value = MagicMock()

        instrumentor = LangGraphInstrumentor()

        with patch.object(instrumentor, '_patch_langgraph'):
            instrumentor._instrument()

        assert instrumentor.graph_wrapper is not None

    @patch("traceai_langchain._langgraph._instrumentor.trace_api")
    def test_instrument_with_custom_tracer_provider(self, mock_trace_api):
        """Test instrument with custom tracer provider."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        mock_tracer = MagicMock()
        mock_trace_api.get_tracer.return_value = mock_tracer

        custom_provider = MagicMock()

        instrumentor = LangGraphInstrumentor()

        with patch.object(instrumentor, '_patch_langgraph'):
            instrumentor._instrument(tracer_provider=custom_provider)

        mock_trace_api.get_tracer.assert_called_with(
            "traceai_langchain._langgraph._instrumentor",
            "0.1.0",
            custom_provider,
        )

    @patch("traceai_langchain._langgraph._instrumentor.trace_api")
    def test_instrument_already_instrumented(self, mock_trace_api):
        """Test that double instrumentation is prevented."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        mock_tracer = MagicMock()
        mock_trace_api.get_tracer.return_value = mock_tracer
        mock_trace_api.get_tracer_provider.return_value = MagicMock()

        instrumentor = LangGraphInstrumentor()

        with patch.object(instrumentor, '_patch_langgraph'):
            instrumentor._instrument()
            # Second call should be no-op
            instrumentor._instrument()

        # get_tracer should only be called once
        assert mock_trace_api.get_tracer.call_count == 1

    @patch("traceai_langchain._langgraph._instrumentor.trace_api")
    def test_uninstrument(self, mock_trace_api):
        """Test uninstrument cleans up."""
        from traceai_langchain._langgraph._instrumentor import LangGraphInstrumentor

        # Reset singleton for testing
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False

        mock_tracer = MagicMock()
        mock_trace_api.get_tracer.return_value = mock_tracer
        mock_trace_api.get_tracer_provider.return_value = MagicMock()

        instrumentor = LangGraphInstrumentor()

        with patch.object(instrumentor, '_patch_langgraph'):
            instrumentor._instrument()

        assert instrumentor.is_instrumented is True

        # Mock langgraph import for uninstrument
        with patch("traceai_langchain._langgraph._instrumentor.StateGraph", create=True):
            with patch.dict('sys.modules', {'langgraph.graph': MagicMock(), 'langgraph.graph.state': MagicMock()}):
                instrumentor._uninstrument()

        assert instrumentor.is_instrumented is False
        assert instrumentor._tracer is None
        assert instrumentor._graph_wrapper is None
        assert instrumentor._state_tracker is None


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
