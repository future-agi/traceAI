"""
Test suite for Groq framework instrumentation.

Tests the instrumentation of Groq components:
- Completions.create (LLM spans)
- AsyncCompletions.create (async LLM spans)
- Protect.protect (guardrail protection)
- Streaming and non-streaming response handling
- Request/response attribute extraction
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from typing import AsyncGenerator
import json

from opentelemetry import trace as trace_api
from opentelemetry.trace.status import StatusCode
from opentelemetry import context as context_api

from traceai_groq import GroqInstrumentor
from traceai_groq._wrappers import (
    _CompletionsWrapper,
    _AsyncCompletionsWrapper,
    _WithTracer,
    _parse_args,
    _flatten,
)
from traceai_groq._utils import (
    _io_value_and_type,
    _as_input_attributes,
    _as_output_attributes,
    _finish_tracing,
    _extract_eval_input,
    _process_response,
    _as_raw_output,
    _as_streaming_output,
    _to_dict,
)
from traceai_groq._request_attributes_extractor import _RequestAttributesExtractor
from traceai_groq._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_groq._with_span import _WithSpan


class TestGroqInstrumentor:
    """Test the main Groq instrumentor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.instrumentor = GroqInstrumentor()

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        deps = self.instrumentor.instrumentation_dependencies()
        assert ("groq >= 0.9.0") == deps

    @patch("traceai_groq.wrap_function_wrapper")
    @patch("traceai_groq.trace_api.get_tracer")
    def test_instrument_basic(self, mock_get_tracer, mock_wrap_function):
        """Test basic instrumentation setup."""
        mock_tracer = Mock()
        mock_get_tracer.return_value = mock_tracer
        
        # Mock the Protect import to avoid ImportError
        with patch("traceai_groq.Protect") as mock_protect:
            mock_protect.protect = Mock()
            
            self.instrumentor._instrument()
        
        # Verify tracer setup
        mock_get_tracer.assert_called_once()
        
        # Verify function wrapping - should wrap completions and protect
        assert mock_wrap_function.call_count >= 2
        
        # Check that originals are stored
        assert hasattr(self.instrumentor, '_original_completions_create')
        assert hasattr(self.instrumentor, '_original_async_completions_create')

    def test_uninstrument(self):
        """Test uninstrumentation."""
        # Set up mock originals
        self.instrumentor._original_completions_create = Mock()
        self.instrumentor._original_async_completions_create = Mock()
        
        with patch("traceai_groq.import_module") as mock_import:
            mock_module = Mock()
            mock_module.Completions = Mock()
            mock_module.AsyncCompletions = Mock()
            mock_import.return_value = mock_module
            
            self.instrumentor._uninstrument()
            
            # Verify originals are restored
            assert mock_module.Completions.create == self.instrumentor._original_completions_create
            assert mock_module.AsyncCompletions.create == self.instrumentor._original_async_completions_create


class TestCompletionsWrapper:
    """Test the sync completions wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.mock_span = MagicMock()
        self.wrapper = _CompletionsWrapper(tracer=self.mock_tracer)

    def test_completions_wrapper_basic(self):
        """Test basic completions wrapper operation."""
        # Mock request parameters
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile",
            "stream": False
        }
        
        # Mock response
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = "Hello! How can I help?"
        
        def wrapped_func(*args, **kwargs):
            return mock_response
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test the wrapper
        result = self.wrapper(wrapped_func, None, (), request_params)
        
        assert result == mock_response
        self.wrapper._start_as_current_span.assert_called_once()

    def test_completions_wrapper_streaming(self):
        """Test streaming completions wrapper operation."""
        # Mock request parameters
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile",
            "stream": True
        }
        
        # Mock streaming response
        mock_chunk1 = Mock()
        mock_chunk1.choices = [{"delta": {"content": "Hello"}}]
        mock_chunk2 = Mock()
        mock_chunk2.choices = [{"delta": {"content": " there!"}}]
        
        def mock_streaming_response():
            yield mock_chunk1
            yield mock_chunk2
        
        def wrapped_func(*args, **kwargs):
            return mock_streaming_response()
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test the wrapper
        result = self.wrapper(wrapped_func, None, (), request_params)
        
        # Consume the generator
        chunks = list(result)
        assert len(chunks) == 2

    def test_completions_wrapper_exception(self):
        """Test completions wrapper exception handling."""
        def wrapped_func(*args, **kwargs):
            raise ValueError("Test error")
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test exception handling
        with pytest.raises(ValueError, match="Test error"):
            self.wrapper(wrapped_func, None, (), {})
        
        # Verify exception was recorded
        mock_with_span.record_exception.assert_called_once()

    def test_completions_wrapper_suppressed(self):
        """Test completions wrapper when instrumentation is suppressed."""
        with patch("traceai_groq._wrappers.context_api.get_value") as mock_get_value:
            mock_get_value.return_value = True
            
            mock_response = Mock()
            def wrapped_func(*args, **kwargs):
                return mock_response
            
            result = self.wrapper(wrapped_func, None, (), {})
            
            assert result == mock_response


class TestAsyncCompletionsWrapper:
    """Test the async completions wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _AsyncCompletionsWrapper(tracer=self.mock_tracer)

    @pytest.mark.asyncio
    async def test_async_completions_wrapper_basic(self):
        """Test basic async completions wrapper operation."""
        # Mock request parameters
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile",
            "stream": False
        }
        
        # Mock response
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = "Hello! How can I help?"
        
        async def wrapped_func(*args, **kwargs):
            return mock_response
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test the wrapper
        result = await self.wrapper(wrapped_func, None, (), request_params)
        
        assert result == mock_response
        self.wrapper._start_as_current_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_completions_wrapper_streaming(self):
        """Test async streaming completions wrapper operation."""
        # Mock request parameters
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile",
            "stream": True
        }
        
        # Mock streaming response
        async def mock_streaming_response():
            yield {"choices": [{"delta": {"content": "Hello"}}]}
            yield {"choices": [{"delta": {"content": " there!"}}]}
        
        async def wrapped_func(*args, **kwargs):
            return mock_streaming_response()
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test the wrapper
        result = await self.wrapper(wrapped_func, None, (), request_params)
        
        # Consume the async generator
        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        assert len(chunks) == 2

    @pytest.mark.asyncio
    async def test_async_completions_wrapper_exception(self):
        """Test async completions wrapper exception handling."""
        async def wrapped_func(*args, **kwargs):
            raise ValueError("Test error")
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test exception handling
        with pytest.raises(ValueError, match="Test error"):
            await self.wrapper(wrapped_func, None, (), {})
        
        # Verify exception was recorded
        mock_with_span.record_exception.assert_called_once()


class TestRequestAttributesExtractor:
    """Test the request attributes extractor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.extractor = _RequestAttributesExtractor()

    def test_get_attributes_from_request(self):
        """Test extracting attributes from request."""
        request_params = {
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"}
            ],
            "model": "llama-3.1-70b-versatile",
            "temperature": 0.7,
            "max_tokens": 100
        }
        
        attributes = list(self.extractor.get_attributes_from_request(request_params))
        
        # Convert to dict for easier testing
        attr_dict = dict(attributes)
        
        # Should contain some attributes (exact keys depend on implementation)
        assert len(attributes) > 0
        # Check for span kind
        assert "fi.span.kind" in attr_dict

    def test_get_extra_attributes_from_request(self):
        """Test extracting extra attributes from request."""
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile"
        }
        
        extra_attributes = list(self.extractor.get_extra_attributes_from_request(request_params))
        
        # Should return valid attributes
        assert isinstance(extra_attributes, list)


class TestResponseAttributesExtractor:
    """Test the response attributes extractor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.extractor = _ResponseAttributesExtractor()

    def test_get_attributes_from_response(self):
        """Test extracting attributes from response."""
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = "Hello! How can I help?"
        mock_response.usage = Mock()
        mock_response.usage.total_tokens = 50
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 40
        
        attributes = list(self.extractor.get_attributes(response=mock_response, is_streaming=False))
        
        # Should contain output attributes
        assert len(attributes) > 0

    def test_get_extra_attributes_from_response(self):
        """Test extracting extra attributes from response."""
        mock_response = Mock()
        request_params = {"model": "llama-3.1-70b-versatile"}
        
        extra_attributes = list(self.extractor.get_extra_attributes(
            response=mock_response,
            request_parameters=request_params,
            is_streaming=False
        ))
        
        # Should return valid attributes
        assert isinstance(extra_attributes, list)


class TestUtilityFunctions:
    """Test utility functions."""

    def test_io_value_and_type_json(self):
        """Test _io_value_and_type with JSON-serializable object."""
        mock_obj = Mock()
        mock_obj.model_dump_json.return_value = '{"test": "value"}'
        
        result = _io_value_and_type(mock_obj)
        
        assert result.value == '{"test": "value"}'
        assert result.type.value == "application/json"

    def test_io_value_and_type_dict(self):
        """Test _io_value_and_type with dict."""
        test_dict = {"test": "value"}
        
        result = _io_value_and_type(test_dict)
        
        assert '"test": "value"' in result.value
        assert result.type.value == "application/json"

    def test_io_value_and_type_string(self):
        """Test _io_value_and_type with string."""
        test_string = "Hello world"
        
        result = _io_value_and_type(test_string)
        
        assert result.value == "Hello world"
        assert result.type.value == "text/plain"

    def test_as_input_attributes(self):
        """Test _as_input_attributes function."""
        from traceai_groq._utils import _ValueAndType
        from fi_instrumentation.fi_types import FiMimeTypeValues
        
        value_and_type = _ValueAndType("test input", FiMimeTypeValues.JSON)
        
        attributes = list(_as_input_attributes(value_and_type))
        
        assert len(attributes) == 2
        attr_dict = dict(attributes)
        assert "input.value" in attr_dict
        assert "input.mime_type" in attr_dict

    def test_as_output_attributes(self):
        """Test _as_output_attributes function."""
        from traceai_groq._utils import _ValueAndType
        from fi_instrumentation.fi_types import FiMimeTypeValues
        
        value_and_type = _ValueAndType("test output", FiMimeTypeValues.TEXT)
        
        attributes = list(_as_output_attributes(value_and_type))
        
        assert len(attributes) == 1  # Only value, no mime_type for TEXT
        attr_dict = dict(attributes)
        assert "output.value" in attr_dict

    def test_extract_eval_input(self):
        """Test _extract_eval_input function."""
        messages = [
            {"content": "First message"},
            {"content": "Second message"},
            {"role": "user", "content": "User message"}
        ]
        
        attributes = list(_extract_eval_input(messages))
        
        attr_dict = dict(attributes)
        assert "eval.input" in attr_dict
        assert "query" in attr_dict

    def test_process_response_dict(self):
        """Test _process_response with dict response."""
        response = {
            "choices": [
                {
                    "message": {
                        "content": "Test response content"
                    }
                }
            ]
        }
        
        result = _process_response(response)
        
        assert result == "Test response content"

    def test_process_response_object(self):
        """Test _process_response with object response."""
        mock_message = Mock()
        mock_message.content = "Test response content"
        
        mock_choice = Mock()
        mock_choice.message = mock_message
        
        mock_response = Mock()
        mock_response.choices = [mock_choice]
        
        result = _process_response(mock_response)
        
        assert result == "Test response content"

    def test_process_response_invalid(self):
        """Test _process_response with invalid response."""
        result = _process_response(None)
        
        assert result is None or result == ""

    def test_as_raw_output(self):
        """Test _as_raw_output function."""
        mock_response = Mock()
        mock_response.model_dump.return_value = {"test": "value"}
        
        attributes = list(_as_raw_output(mock_response))
        
        assert len(attributes) == 1
        key, value = attributes[0]
        assert key.endswith("output")  
        assert '"test": "value"' in value

    def test_as_streaming_output(self):
        """Test _as_streaming_output function."""
        # Mock ChatCompletionChunk objects
        mock_chunk1 = Mock()
        mock_chunk1.to_dict.return_value = {
            "choices": [{"delta": {"content": "Hello"}}],
            "model": "llama-3.1-70b-versatile"
        }
        
        mock_chunk2 = Mock()
        mock_chunk2.to_dict.return_value = {
            "choices": [{"delta": {"content": " world!"}}],
            "x_groq": {
                "usage": {
                    "total_tokens": 50,
                    "prompt_tokens": 10,
                    "completion_tokens": 40
                }
            },
            "model": "llama-3.1-70b-versatile"
        }
        
        chunks = [mock_chunk1, mock_chunk2]
        
        attributes = list(_as_streaming_output(chunks))
        
        attr_dict = dict(attributes)
        assert "output.value" in attr_dict
        assert attr_dict["output.value"] == "Hello world!"
        assert "llm.model_name" in attr_dict
        assert "llm.token_count.total" in attr_dict

    def test_to_dict_with_model_dump(self):
        """Test _to_dict with object that has __dict__."""
        class SimpleObj:
            def __init__(self):
                self.test = "value"
                self.number = 42
        
        obj = SimpleObj()
        result = _to_dict(obj)
        
        assert result == {"test": "value", "number": 42}

    def test_to_dict_with_dict(self):
        """Test _to_dict with dict object."""
        test_dict = {"test": "value"}
        
        result = _to_dict(test_dict)
        
        assert result == {"test": "value"}


class TestWrapperUtilities:
    """Test wrapper utility functions."""

    def test_parse_args(self):
        """Test _parse_args function."""
        from inspect import signature
        
        def test_func(arg1, arg2="default", *args, **kwargs):
            pass
        
        sig = signature(test_func)
        result = _parse_args(sig, "value1", "value2", extra_kwarg="extra")
        
        expected = {
            "arg1": "value1",
            "arg2": "value2",
            "args": (),
            "kwargs": {"extra_kwarg": "extra"}
        }
        
        assert result == expected

    def test_flatten_nested_dict(self):
        """Test _flatten function with nested dict."""
        test_dict = {
            "level1": {
                "level2": "value",
                "level2b": None  # Should be skipped
            },
            "simple": "simple_value"
        }
        
        flattened = list(_flatten(test_dict))
        
        attr_dict = dict(flattened)
        assert "level1.level2" in attr_dict
        assert attr_dict["level1.level2"] == "value"
        assert "simple" in attr_dict
        assert attr_dict["simple"] == "simple_value"
        assert "level1.level2b" not in attr_dict  # None values excluded

    def test_flatten_list_of_dicts(self):
        """Test _flatten function with list of dicts."""
        test_dict = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"}
            ]
        }
        
        flattened = list(_flatten(test_dict))
        
        attr_dict = dict(flattened)
        assert "messages.0.role" in attr_dict
        assert attr_dict["messages.0.role"] == "user"
        assert "messages.1.content" in attr_dict
        assert attr_dict["messages.1.content"] == "Hi there"

    def test_flatten_enum_values(self):
        """Test _flatten function with enum values."""
        from enum import Enum
        
        class TestEnum(Enum):
            VALUE = "test_value"
        
        test_dict = {
            "enum_field": TestEnum.VALUE
        }
        
        flattened = list(_flatten(test_dict))
        
        attr_dict = dict(flattened)
        assert "enum_field" in attr_dict
        assert attr_dict["enum_field"] == "test_value"


class TestWithSpan:
    """Test _WithSpan utility class."""

    def test_with_span_creation(self):
        """Test _WithSpan creation and basic functionality."""
        mock_span = Mock()
        context_attrs = {"context": "value"}
        extra_attrs = {"extra": "value"}
        
        with_span = _WithSpan(
            span=mock_span,
            context_attributes=context_attrs,
            extra_attributes=extra_attrs
        )
        
        assert with_span._span == mock_span
        assert hasattr(with_span, '_context_attributes') or hasattr(with_span, 'context_attributes')
        assert hasattr(with_span, '_extra_attributes') or hasattr(with_span, 'extra_attributes')


if __name__ == "__main__":
    pytest.main([__file__]) 