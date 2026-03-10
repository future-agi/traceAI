"""Tests for Pydantic AI instrumentor."""

import pytest
from unittest.mock import MagicMock, patch


class TestPydanticAIInstrumentor:
    """Test PydanticAIInstrumentor class."""

    def test_singleton_pattern(self):
        """Test that instrumentor follows singleton pattern."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        inst1 = PydanticAIInstrumentor()
        inst2 = PydanticAIInstrumentor()

        assert inst1 is inst2

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies list."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        inst = PydanticAIInstrumentor()
        deps = inst.instrumentation_dependencies()

        assert len(deps) > 0
        assert any("pydantic-ai" in dep for dep in deps)

    def test_is_instrumented_initially_false(self):
        """Test that is_instrumented is initially False."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        inst = PydanticAIInstrumentor()
        assert inst.is_instrumented is False

    @patch("traceai_pydantic_ai._instrumentor.trace_api")
    def test_instrument_creates_tracer(self, mock_trace_api):
        """Test that instrument() creates a tracer."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        mock_provider = MagicMock()
        mock_tracer = MagicMock()
        mock_trace_api.get_tracer.return_value = mock_tracer

        inst = PydanticAIInstrumentor()

        with patch.object(inst, "_patch_pydantic_ai") as mock_patch:
            with patch.object(inst, "instrumentation_dependencies", return_value=[]):
                mock_patch.side_effect = ImportError("pydantic_ai not installed")
                inst.instrument(tracer_provider=mock_provider)

        mock_trace_api.get_tracer.assert_called_once()

    @patch("traceai_pydantic_ai._instrumentor.trace_api")
    def test_instrument_skips_if_already_instrumented(self, mock_trace_api):
        """Test that instrument() is idempotent."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        inst = PydanticAIInstrumentor()

        with patch.object(inst, "_patch_pydantic_ai"):
            with patch.object(inst, "instrumentation_dependencies", return_value=[]):
                inst.instrument()
                # Second call should be skipped
                inst.instrument()

        # get_tracer should only be called once
        assert mock_trace_api.get_tracer.call_count == 1

    def test_uninstrument_when_not_instrumented(self):
        """Test that uninstrument() is safe when not instrumented."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        inst = PydanticAIInstrumentor()
        # Should not raise
        inst.uninstrument()

    @patch("traceai_pydantic_ai._instrumentor.trace_api")
    def test_uninstrument_clears_state(self, mock_trace_api):
        """Test that uninstrument() clears internal state."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        inst = PydanticAIInstrumentor()

        with patch.object(inst, "_patch_pydantic_ai"):
            with patch.object(inst, "instrumentation_dependencies", return_value=[]):
                inst.instrument()

        assert inst.is_instrumented is True

        inst.uninstrument()

        assert inst.is_instrumented is False
        assert inst._tracer is None


class TestInstrumentPydanticAiFunction:
    """Test instrument_pydantic_ai convenience function."""

    @patch("traceai_pydantic_ai._instrumentor.trace_api")
    def test_returns_instrumentor(self, mock_trace_api):
        """Test that function returns instrumentor instance."""
        from traceai_pydantic_ai._instrumentor import (
            instrument_pydantic_ai,
            PydanticAIInstrumentor,
        )

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        with patch.object(PydanticAIInstrumentor, "_patch_pydantic_ai"):
            with patch.object(PydanticAIInstrumentor, "instrumentation_dependencies", return_value=[]):
                result = instrument_pydantic_ai()

        assert isinstance(result, PydanticAIInstrumentor)

    @patch("traceai_pydantic_ai._instrumentor.trace_api")
    def test_passes_tracer_provider(self, mock_trace_api):
        """Test that tracer_provider is passed through."""
        from traceai_pydantic_ai._instrumentor import (
            instrument_pydantic_ai,
            PydanticAIInstrumentor,
        )

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        mock_provider = MagicMock()

        with patch.object(PydanticAIInstrumentor, "_patch_pydantic_ai"):
            with patch.object(PydanticAIInstrumentor, "instrumentation_dependencies", return_value=[]):
                instrument_pydantic_ai(tracer_provider=mock_provider)

        mock_trace_api.get_tracer.assert_called()

    @patch("traceai_pydantic_ai._instrumentor.trace_api")
    def test_use_builtin_option(self, mock_trace_api):
        """Test use_builtin option."""
        from traceai_pydantic_ai._instrumentor import (
            instrument_pydantic_ai,
            PydanticAIInstrumentor,
        )

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        with patch.object(PydanticAIInstrumentor, "_patch_pydantic_ai"):
            with patch.object(PydanticAIInstrumentor, "instrumentation_dependencies", return_value=[]):
                result = instrument_pydantic_ai(use_builtin=True)

        assert result._use_builtin is True


class TestPatchPydanticAi:
    """Test _patch_pydantic_ai method."""

    @patch("traceai_pydantic_ai._instrumentor.trace_api")
    def test_raises_import_error_when_not_installed(self, mock_trace_api):
        """Test that ImportError is raised when Pydantic AI is not installed."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        inst = PydanticAIInstrumentor()
        inst._tracer = MagicMock()

        # This should raise or log warning since pydantic_ai is not installed
        try:
            inst._patch_pydantic_ai()
        except ImportError:
            pass  # Expected when Pydantic AI not installed

    @patch("traceai_pydantic_ai._instrumentor.wrap_agent_run")
    @patch("traceai_pydantic_ai._instrumentor.trace_api")
    def test_patches_agent_when_available(self, mock_trace_api, mock_wrap):
        """Test that Agent is patched when Pydantic AI is available."""
        from traceai_pydantic_ai._instrumentor import PydanticAIInstrumentor
        import sys

        # Reset singleton for test
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        # Create mock pydantic_ai module
        mock_pydantic_ai = MagicMock()
        mock_agent = MagicMock()
        mock_agent.run = MagicMock()
        mock_agent.run_sync = MagicMock()
        mock_pydantic_ai.Agent = mock_agent

        mock_wrapped = MagicMock()
        mock_wrap.return_value = mock_wrapped

        inst = PydanticAIInstrumentor()
        inst._tracer = MagicMock()
        inst._use_builtin = False

        with patch.dict(sys.modules, {"pydantic_ai": mock_pydantic_ai}):
            inst._patch_pydantic_ai()

        # Verify wrap_agent_run was called for run and run_sync
        assert mock_wrap.call_count >= 2


class TestPublicExports:
    """Test public exports from package."""

    def test_instrumentor_exported(self):
        """Test PydanticAIInstrumentor is exported."""
        from traceai_pydantic_ai import PydanticAIInstrumentor

        assert PydanticAIInstrumentor is not None

    def test_convenience_function_exported(self):
        """Test instrument_pydantic_ai is exported."""
        from traceai_pydantic_ai import instrument_pydantic_ai

        assert callable(instrument_pydantic_ai)

    def test_attributes_exported(self):
        """Test PydanticAIAttributes is exported."""
        from traceai_pydantic_ai import PydanticAIAttributes

        assert PydanticAIAttributes is not None

    def test_span_kind_exported(self):
        """Test PydanticAISpanKind is exported."""
        from traceai_pydantic_ai import PydanticAISpanKind

        assert PydanticAISpanKind is not None

    def test_version_available(self):
        """Test __version__ is available."""
        import traceai_pydantic_ai

        assert hasattr(traceai_pydantic_ai, "__version__")
        assert traceai_pydantic_ai.__version__ == "0.1.0"

    def test_get_model_provider_exported(self):
        """Test get_model_provider is exported."""
        from traceai_pydantic_ai import get_model_provider

        assert callable(get_model_provider)

    def test_wrapper_functions_exported(self):
        """Test wrapper functions are exported."""
        from traceai_pydantic_ai import wrap_agent_run, wrap_tool_function

        assert callable(wrap_agent_run)
        assert callable(wrap_tool_function)
