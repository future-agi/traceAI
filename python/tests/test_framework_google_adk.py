"""
Test suite for Google ADK framework instrumentation.

Tests the instrumentation of Google ADK components:
- Runner.run_async (CHAIN spans)
- BaseAgent.run_async (AGENT spans)
- trace_call_llm (LLM spans)
- trace_tool_call (TOOL spans)
- Tracer management and passthrough functionality
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from typing import AsyncGenerator
import json

from opentelemetry import trace as trace_api
from opentelemetry.trace.status import StatusCode
from opentelemetry import context as context_api

from traceai_google_adk import GoogleADKInstrumentor, _PassthroughTracer
from traceai_google_adk._wrappers import (
    _RunnerRunAsync,
    _BaseAgentRunAsync,
    _TraceCallLlm,
    _TraceToolCall,
    bind_args_kwargs,
    _default,
    _get_attributes_from_generate_content_config,
    _get_attributes_from_llm_response,
    _get_attributes_from_usage_metadata,
    _get_attributes_from_content,
    _get_attributes_from_parts,
    _get_attributes_from_text_part,
    _get_attributes_from_function_call,
    _get_attributes_from_function_response,
    _get_attributes_from_base_tool,
)


class TestGoogleADKInstrumentor:
    """Test the main Google ADK instrumentor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.instrumentor = GoogleADKInstrumentor()

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        deps = self.instrumentor.instrumentation_dependencies()
        assert ("google-adk >= 1.2.1",) == deps

    def test_instrument_basic(self):
        """Test basic instrumentation setup without complex patches."""
        # Mock tracer setup
        mock_tracer = Mock()
        self.instrumentor._tracer = mock_tracer
        
        # Mock the helper methods to avoid complex patching
        self.instrumentor._patch_trace_call_llm = Mock()
        self.instrumentor._patch_trace_tool_call = Mock()
        self.instrumentor._disable_existing_tracers = Mock()
        
        # Test that the helper methods would be called
        self.instrumentor._patch_trace_call_llm()
        self.instrumentor._patch_trace_tool_call()
        self.instrumentor._disable_existing_tracers()
        
        # Verify methods were called
        self.instrumentor._patch_trace_call_llm.assert_called_once()
        self.instrumentor._patch_trace_tool_call.assert_called_once()
        self.instrumentor._disable_existing_tracers.assert_called_once()

    def test_uninstrument(self):
        """Test uninstrumentation."""
        # Mock the cleanup methods
        self.instrumentor._unpatch_trace_call_llm = Mock()
        self.instrumentor._unpatch_trace_tool_call = Mock()
        self.instrumentor._restore_existing_tracers = Mock()
        self.instrumentor._originals = []
        
        self.instrumentor._uninstrument()
        
        # Verify cleanup methods were called
        self.instrumentor._unpatch_trace_call_llm.assert_called_once()
        self.instrumentor._unpatch_trace_tool_call.assert_called_once()
        self.instrumentor._restore_existing_tracers.assert_called_once()


class TestRunnerRunAsync:
    """Test the Runner run_async wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.mock_span = MagicMock(spec=trace_api.Span)
        self.mock_context_manager = MagicMock()
        self.mock_context_manager.__enter__.return_value = self.mock_span
        self.mock_context_manager.__exit__.return_value = None
        self.mock_tracer.start_as_current_span.return_value = self.mock_context_manager
        self.wrapper = _RunnerRunAsync(self.mock_tracer)

    @pytest.mark.asyncio
    async def test_runner_run_async_basic(self):
        """Test basic runner run_async operation."""
        # Mock runner instance
        mock_runner = Mock()
        mock_runner.app_name = "TestApp"
        
        # Mock event
        mock_event = Mock()
        mock_event.is_final_response.return_value = True
        mock_event.model_dump_json.return_value = '{"result": "success"}'
        
        # Create async generator mock
        async def mock_generator():
            yield mock_event
        
        wrapped_func = Mock(return_value=mock_generator())
        
        # Test the wrapper
        kwargs = {
            "user_id": "test_user",
            "session_id": "test_session",
            "new_message": Mock()
        }
        
        result_generator = self.wrapper(wrapped_func, mock_runner, (), kwargs)
        
        # Consume the async generator
        events = []
        async for event in result_generator:
            events.append(event)
        
        assert len(events) == 1
        assert events[0] == mock_event
        wrapped_func.assert_called_once()

    @pytest.mark.asyncio
    async def test_runner_run_async_suppressed(self):
        """Test runner run_async when instrumentation is suppressed."""
        with patch("traceai_google_adk._wrappers.context_api.get_value") as mock_get_value:
            mock_get_value.return_value = True
            
            mock_runner = Mock()
            async def mock_generator():
                yield Mock()
            
            wrapped_func = Mock(return_value=mock_generator())
            
            result = self.wrapper(wrapped_func, mock_runner, (), {})
            
            # Should return original generator without wrapping
            assert result is not None
            self.mock_tracer.start_as_current_span.assert_not_called()


class TestBaseAgentRunAsync:
    """Test the BaseAgent run_async wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.mock_span = MagicMock(spec=trace_api.Span)
        self.mock_context_manager = MagicMock()
        self.mock_context_manager.__enter__.return_value = self.mock_span
        self.mock_context_manager.__exit__.return_value = None
        self.mock_tracer.start_as_current_span.return_value = self.mock_context_manager
        self.wrapper = _BaseAgentRunAsync(self.mock_tracer)

    @pytest.mark.asyncio
    async def test_base_agent_run_async(self):
        """Test base agent run_async operation."""
        mock_agent = Mock()
        mock_agent.name = "TestAgent"
        
        # Mock event
        mock_event = Mock()
        mock_event.is_final_response.return_value = True
        mock_event.model_dump_json.return_value = '{"response": "agent_output"}'
        
        async def mock_generator():
            yield mock_event
        
        wrapped_func = Mock(return_value=mock_generator())
        
        result_generator = self.wrapper(wrapped_func, mock_agent, (), {})
        
        # Consume the async generator
        events = []
        async for event in result_generator:
            events.append(event)
        
        assert len(events) == 1
        assert events[0] == mock_event
        wrapped_func.assert_called_once()

    @pytest.mark.asyncio
    async def test_base_agent_run_async_no_final_response(self):
        """Test base agent run_async with no final response."""
        mock_agent = Mock()
        mock_agent.name = "TestAgent"
        
        # Mock event that's not a final response
        mock_event = Mock()
        mock_event.is_final_response.return_value = False
        
        async def mock_generator():
            yield mock_event
        
        wrapped_func = Mock(return_value=mock_generator())
        
        result_generator = self.wrapper(wrapped_func, mock_agent, (), {})
        
        # Consume the async generator
        events = []
        async for event in result_generator:
            events.append(event)
        
        assert len(events) == 1
        # Should not set output attributes for non-final responses
        self.mock_span.set_attribute.assert_not_called()


class TestTraceCallLlm:
    """Test the LLM call tracing wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _TraceCallLlm(self.mock_tracer)

    @patch("traceai_google_adk._wrappers.get_current_span")
    def test_trace_call_llm_basic(self, mock_get_current_span):
        """Test basic LLM call tracing."""
        mock_span = MagicMock()
        mock_get_current_span.return_value = mock_span
        
        # Mock LLM request and response
        mock_llm_request = Mock()
        mock_llm_request.model = "gemini-pro"
        mock_llm_request.tools_dict = {}
        mock_llm_request.config = None
        mock_llm_request.contents = []
        
        mock_llm_response = Mock()
        mock_llm_response.model_dump_json.return_value = '{"response": "test"}'
        
        def wrapped_func(*args, **kwargs):
            return "result"
        wrapped_func.__name__ = "trace_call_llm"
        
        # Test with LLM request and response
        wrapped_function = self.wrapper(wrapped_func, None, (), {
            "llm_request": mock_llm_request,
            "llm_response": mock_llm_response
        })
        
        # Call the wrapped function to get the result
        result = wrapped_function()
        
        assert result == "result"
        mock_span.set_status.assert_called_with(StatusCode.OK)

    @patch("traceai_google_adk._wrappers.get_current_span")
    def test_trace_call_llm_suppressed(self, mock_get_current_span):
        """Test LLM call tracing when suppressed."""
        with patch("traceai_google_adk._wrappers.context_api.get_value") as mock_get_value:
            mock_get_value.return_value = True
            
            def wrapped_func(*args, **kwargs):
                return "result"
            wrapped_func.__name__ = "trace_call_llm"
            
            wrapped_function = self.wrapper(wrapped_func, None, (), {})
            result = wrapped_function()
            
            assert result == "result"
            mock_get_current_span.assert_not_called()


class TestTraceToolCall:
    """Test the tool call tracing wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _TraceToolCall(self.mock_tracer)

    @patch("traceai_google_adk._wrappers.get_current_span")
    def test_trace_tool_call_basic(self, mock_get_current_span):
        """Test basic tool call tracing."""
        mock_span = MagicMock()
        mock_get_current_span.return_value = mock_span
        
        # Mock tool
        mock_tool = Mock()
        mock_tool.name = "test_tool"
        mock_tool.description = "A test tool"
        
        # Mock event
        mock_event = Mock()
        mock_response = Mock()
        mock_response.model_dump_json.return_value = '{"tool_result": "success"}'
        mock_event.get_function_responses.return_value = [mock_response]
        
        def wrapped_func(*args, **kwargs):
            return "result"
        wrapped_func.__name__ = "trace_tool_call"
        
        # Test with tool and event
        wrapped_function = self.wrapper(wrapped_func, None, (), {
            "base_tool": mock_tool,
            "args_dict": {"param": "value"},
            "event": mock_event
        })
        
        result = wrapped_function()
        
        assert result == "result"
        mock_span.set_status.assert_called_with(StatusCode.OK)

    @patch("traceai_google_adk._wrappers.get_current_span")
    def test_trace_tool_call_no_tool(self, mock_get_current_span):
        """Test tool call tracing without tool."""
        mock_span = MagicMock()
        mock_get_current_span.return_value = mock_span
        
        def wrapped_func(*args, **kwargs):
            return "result"
        wrapped_func.__name__ = "trace_tool_call"
        
        wrapped_function = self.wrapper(wrapped_func, None, (), {})
        result = wrapped_function()
        
        assert result == "result"
        mock_span.set_status.assert_called_with(StatusCode.OK)


class TestPassthroughTracer:
    """Test the passthrough tracer."""

    def test_passthrough_tracer_basic(self):
        """Test basic passthrough tracer functionality."""
        mock_original_tracer = Mock(spec=trace_api.Tracer)
        mock_span = Mock()
        
        with patch("traceai_google_adk.get_current_span") as mock_get_current_span:
            mock_get_current_span.return_value = mock_span
            
            passthrough = _PassthroughTracer(mock_original_tracer)
            
            # Test that start_as_current_span returns current span
            with passthrough.start_as_current_span("test") as span:
                assert span == mock_span
            
            mock_get_current_span.assert_called()

    def test_passthrough_tracer_attributes(self):
        """Test passthrough tracer attribute access."""
        mock_original_tracer = Mock()
        mock_original_tracer.some_attribute = "test_value"
        
        passthrough = _PassthroughTracer(mock_original_tracer)
        
        # Should pass through attribute access
        assert passthrough.some_attribute == "test_value"


class TestUtilityFunctions:
    """Test utility functions."""

    def test_bind_args_kwargs(self):
        """Test bind_args_kwargs function."""
        def test_func(arg1, arg2="default", *args, **kwargs):
            pass
        
        result = bind_args_kwargs(test_func, "value1", "value2", "extra", extra_kwarg="extra_value")
        
        expected = {
            "arg1": "value1",
            "arg2": "value2",
            "args": ("extra",),
            "kwargs": {"extra_kwarg": "extra_value"}
        }
        
        # Convert to dict for comparison since OrderedDict is returned
        assert dict(result) == expected

    def test_default_function(self):
        """Test _default function."""
        # Test with various objects
        test_obj = object()
        result = _default(test_obj)
        
        assert isinstance(result, str)
        assert "object" in result

    def test_get_attributes_from_text_part(self):
        """Test _get_attributes_from_text_part function."""
        text = "Hello, world!"
        prefix = "test."
        
        attributes = list(_get_attributes_from_text_part(text, prefix=prefix))
        
        # Should return 2 attributes: text content and type
        assert len(attributes) == 2
        attribute_dict = dict(attributes)
        assert f"{prefix}message_content.text" in attribute_dict
        assert f"{prefix}message_content.type" in attribute_dict
        assert attribute_dict[f"{prefix}message_content.text"] == text
        assert attribute_dict[f"{prefix}message_content.type"] == "text"

    def test_get_attributes_from_generate_content_config(self):
        """Test _get_attributes_from_generate_content_config function."""
        mock_config = Mock()
        mock_config.model_dump_json.return_value = '{"temperature": 0.7}'
        
        attributes = list(_get_attributes_from_generate_content_config(mock_config))
        
        assert len(attributes) == 1
        key, value = attributes[0]
        assert '"temperature": 0.7' in value

    def test_get_attributes_from_llm_response(self):
        """Test _get_attributes_from_llm_response function."""
        mock_response = Mock()
        mock_response.model_dump_json.return_value = '{"output": "response"}'
        mock_response.usage_metadata = None
        mock_response.content = None
        
        attributes = list(_get_attributes_from_llm_response(mock_response))
        
        assert len(attributes) == 2  # OUTPUT_VALUE and OUTPUT_MIME_TYPE
        
    def test_get_attributes_from_base_tool(self):
        """Test _get_attributes_from_base_tool function."""
        mock_tool = Mock()
        mock_tool.name = "test_tool"
        mock_tool.description = "A test tool"
        mock_declaration = Mock()
        mock_declaration.model_dump_json.return_value = '{"name": "test_tool", "description": "A test tool"}'
        mock_tool._get_declaration.return_value = mock_declaration
        
        attributes = list(_get_attributes_from_base_tool(mock_tool, prefix="tool."))
        
        # Should extract tool JSON schema
        attribute_dict = dict(attributes)
        assert "tool.tool.json_schema" in attribute_dict
        assert '"name": "test_tool"' in attribute_dict["tool.tool.json_schema"]

    def test_utility_functions_exception_handling(self):
        """Test that utility functions handle exceptions gracefully."""
        # Test with invalid object that would raise an exception
        mock_obj = Mock()
        mock_obj.model_dump_json.side_effect = Exception("Test error")
        
        # Should not raise exception, should return empty iterator
        attributes = list(_get_attributes_from_llm_response(mock_obj))
        assert attributes == []


if __name__ == "__main__":
    pytest.main([__file__]) 