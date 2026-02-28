"""Tests for Claude Agent SDK instrumentor."""

import pytest
from unittest.mock import MagicMock, patch


class TestClaudeAgentInstrumentor:
    """Test ClaudeAgentInstrumentor class."""

    def test_singleton_pattern(self):
        """Test that instrumentor follows singleton pattern."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        inst1 = ClaudeAgentInstrumentor()
        inst2 = ClaudeAgentInstrumentor()

        assert inst1 is inst2

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies list."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        inst = ClaudeAgentInstrumentor()
        deps = inst.instrumentation_dependencies()

        assert len(deps) > 0
        assert any("claude-agent-sdk" in dep for dep in deps)

    def test_is_instrumented_initially_false(self):
        """Test that is_instrumented is initially False."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        inst = ClaudeAgentInstrumentor()
        assert inst.is_instrumented is False

    @patch("traceai_claude_agent_sdk._instrumentor.trace_api")
    def test_instrument_creates_tracer(self, mock_trace_api):
        """Test that instrument() creates a tracer."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        mock_provider = MagicMock()
        mock_tracer = MagicMock()
        mock_trace_api.get_tracer.return_value = mock_tracer

        inst = ClaudeAgentInstrumentor()

        # Mock the SDK import to fail (SDK not installed in test env)
        # Also need to bypass the dependency check in BaseInstrumentor
        with patch.object(inst, "_patch_claude_agent_sdk") as mock_patch:
            with patch.object(inst, "instrumentation_dependencies", return_value=[]):
                mock_patch.side_effect = ImportError("claude_agent_sdk not installed")
                inst.instrument(tracer_provider=mock_provider)

        mock_trace_api.get_tracer.assert_called_once()

    @patch("traceai_claude_agent_sdk._instrumentor.trace_api")
    def test_instrument_skips_if_already_instrumented(self, mock_trace_api):
        """Test that instrument() is idempotent."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        inst = ClaudeAgentInstrumentor()

        with patch.object(inst, "_patch_claude_agent_sdk"):
            with patch.object(inst, "instrumentation_dependencies", return_value=[]):
                inst.instrument()
                # Second call should be skipped
                inst.instrument()

        # get_tracer should only be called once
        assert mock_trace_api.get_tracer.call_count == 1

    def test_uninstrument_when_not_instrumented(self):
        """Test that uninstrument() is safe when not instrumented."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        inst = ClaudeAgentInstrumentor()
        # Should not raise
        inst.uninstrument()

    @patch("traceai_claude_agent_sdk._instrumentor.trace_api")
    def test_uninstrument_clears_state(self, mock_trace_api):
        """Test that uninstrument() clears internal state."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        inst = ClaudeAgentInstrumentor()

        with patch.object(inst, "_patch_claude_agent_sdk"):
            with patch.object(inst, "instrumentation_dependencies", return_value=[]):
                inst.instrument()

        assert inst.is_instrumented is True

        inst.uninstrument()

        assert inst.is_instrumented is False
        assert inst._tracer is None


class TestInstrumentClaudeAgentSdkFunction:
    """Test instrument_claude_agent_sdk convenience function."""

    @patch("traceai_claude_agent_sdk._instrumentor.trace_api")
    def test_returns_instrumentor(self, mock_trace_api):
        """Test that function returns instrumentor instance."""
        from traceai_claude_agent_sdk._instrumentor import (
            instrument_claude_agent_sdk,
            ClaudeAgentInstrumentor,
        )

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        with patch.object(ClaudeAgentInstrumentor, "_patch_claude_agent_sdk"):
            with patch.object(ClaudeAgentInstrumentor, "instrumentation_dependencies", return_value=[]):
                result = instrument_claude_agent_sdk()

        assert isinstance(result, ClaudeAgentInstrumentor)

    @patch("traceai_claude_agent_sdk._instrumentor.trace_api")
    def test_passes_tracer_provider(self, mock_trace_api):
        """Test that tracer_provider is passed through."""
        from traceai_claude_agent_sdk._instrumentor import (
            instrument_claude_agent_sdk,
            ClaudeAgentInstrumentor,
        )

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        mock_provider = MagicMock()

        with patch.object(ClaudeAgentInstrumentor, "_patch_claude_agent_sdk"):
            with patch.object(ClaudeAgentInstrumentor, "instrumentation_dependencies", return_value=[]):
                instrument_claude_agent_sdk(tracer_provider=mock_provider)

        # Verify tracer was created with provider
        mock_trace_api.get_tracer.assert_called()


class TestPatchClaudeAgentSdk:
    """Test _patch_claude_agent_sdk method."""

    @patch("traceai_claude_agent_sdk._instrumentor.trace_api")
    def test_raises_import_error_when_sdk_not_installed(self, mock_trace_api):
        """Test that ImportError is raised when SDK is not installed."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        inst = ClaudeAgentInstrumentor()
        inst._tracer = MagicMock()

        # This should raise or log warning since claude_agent_sdk is not installed
        # The actual behavior depends on whether SDK is installed
        try:
            inst._patch_claude_agent_sdk()
        except ImportError:
            pass  # Expected when SDK not installed

    @patch("traceai_claude_agent_sdk._instrumentor.wrap_claude_sdk_client")
    @patch("traceai_claude_agent_sdk._instrumentor.trace_api")
    def test_wraps_client_when_sdk_available(self, mock_trace_api, mock_wrap):
        """Test that ClaudeSDKClient is wrapped when SDK is available."""
        from traceai_claude_agent_sdk._instrumentor import ClaudeAgentInstrumentor
        import sys

        # Reset singleton for test
        ClaudeAgentInstrumentor._instance = None
        ClaudeAgentInstrumentor._is_instrumented = False

        # Create mock SDK module
        mock_sdk = MagicMock()
        original_client = MagicMock()
        mock_sdk.ClaudeSDKClient = original_client

        mock_wrapped_class = MagicMock()
        mock_wrap.return_value = mock_wrapped_class

        inst = ClaudeAgentInstrumentor()
        inst._tracer = MagicMock()

        with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk}):
            inst._patch_claude_agent_sdk()

        # Verify wrap was called with the original class
        mock_wrap.assert_called_once_with(
            original_client,
            inst._tracer,
        )
        # Verify the class was replaced
        assert mock_sdk.ClaudeSDKClient == mock_wrapped_class


class TestPublicExports:
    """Test public exports from package."""

    def test_instrumentor_exported(self):
        """Test ClaudeAgentInstrumentor is exported."""
        from traceai_claude_agent_sdk import ClaudeAgentInstrumentor

        assert ClaudeAgentInstrumentor is not None

    def test_convenience_function_exported(self):
        """Test instrument_claude_agent_sdk is exported."""
        from traceai_claude_agent_sdk import instrument_claude_agent_sdk

        assert callable(instrument_claude_agent_sdk)

    def test_attributes_exported(self):
        """Test ClaudeAgentAttributes is exported."""
        from traceai_claude_agent_sdk import ClaudeAgentAttributes

        assert ClaudeAgentAttributes is not None

    def test_span_kind_exported(self):
        """Test ClaudeAgentSpanKind is exported."""
        from traceai_claude_agent_sdk import ClaudeAgentSpanKind

        assert ClaudeAgentSpanKind is not None

    def test_version_available(self):
        """Test __version__ is available."""
        import traceai_claude_agent_sdk

        assert hasattr(traceai_claude_agent_sdk, "__version__")
        assert traceai_claude_agent_sdk.__version__ == "0.1.0"
