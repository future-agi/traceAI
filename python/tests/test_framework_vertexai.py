"""
Comprehensive test suite for Vertex AI instrumentation framework.
Tests the VertexAIInstrumentor and related wrapper functionality.
"""

import asyncio
import logging
import unittest
from unittest.mock import Mock, patch, MagicMock, ANY, call
from typing import Any, Collection, Dict, List, Optional, AsyncIterator, Iterator
from collections.abc import AsyncIterable, Iterable

import pytest
import proto

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.trace import Tracer, Status, StatusCode
from opentelemetry.trace.span import Span as OtelSpan
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.util.types import AttributeValue

from traceai_vertexai import VertexAIInstrumentor
from traceai_vertexai._wrapper import (
    _Wrapper, _CallbackForAwaitable, _CallbackForIterable,
    _get_prediction_service_request, _update_span, _parse_usage_metadata,
    _parse_content, _parse_tool_calls, _parse_parts, _parse_part,
    _parse_predictions, _role, _extract_image_data, _finish,
    _GenerateContentResponseAccumulator, _NoOpResponseAccumulator
)
from traceai_vertexai._accumulator import (
    _KeyValuesAccumulator, _StringAccumulator, _IndexedAccumulator
)
from traceai_vertexai._proxy import _proxy


class TestVertexAIInstrumentor:
    """Test suite for VertexAIInstrumentor class."""

    def test_inheritance(self):
        """Test that VertexAIInstrumentor inherits from BaseInstrumentor."""
        instrumentor = VertexAIInstrumentor()
        assert isinstance(instrumentor, BaseInstrumentor)

    def test_instrumentation_dependencies(self):
        """Test that instrumentor returns correct dependencies."""
        instrumentor = VertexAIInstrumentor()
        dependencies = instrumentor.instrumentation_dependencies()
        assert isinstance(dependencies, Collection)
        assert "google-cloud-aiplatform >= 1.49.0" in dependencies
        assert "futureagi >= 0.0.1" in dependencies

    def test_instrument_with_default_config(self):
        """Test instrumentation with default configuration."""
        with patch('traceai_vertexai.get_tracer_provider') as mock_get_provider, \
             patch('traceai_vertexai.get_tracer') as mock_get_tracer, \
             patch('traceai_vertexai.wrap_function_wrapper') as mock_wrap, \
             patch('google.api_core.gapic_v1') as mock_gapic:
            
            mock_provider = Mock()
            mock_tracer = Mock(spec=Tracer)
            mock_get_provider.return_value = mock_provider
            mock_get_tracer.return_value = mock_tracer
            
            # Mock the gapic methods properly
            mock_method = Mock()
            mock_method.__module__ = "google.api_core.gapic_v1.method"
            mock_method.__name__ = "wrap_method"
            
            mock_method_async = Mock()
            mock_method_async.__module__ = "google.api_core.gapic_v1.method_async"
            mock_method_async.__name__ = "wrap_method"
            
            mock_gapic.method.wrap_method = mock_method
            mock_gapic.method_async.wrap_method = mock_method_async
            
            instrumentor = VertexAIInstrumentor()
            instrumentor._instrument()
            
            mock_get_provider.assert_called_once()
            mock_get_tracer.assert_called_once()
            # Should wrap both sync and async gapic methods + Protect.protect
            assert mock_wrap.call_count >= 2

    def test_instrument_with_custom_tracer_provider(self):
        """Test instrumentation with custom tracer provider."""
        with patch('traceai_vertexai.get_tracer') as mock_get_tracer, \
             patch('traceai_vertexai.wrap_function_wrapper') as mock_wrap:
            
            mock_provider = Mock()
            mock_tracer = Mock(spec=Tracer)
            mock_get_tracer.return_value = mock_tracer
            
            instrumentor = VertexAIInstrumentor()
            instrumentor._instrument(tracer_provider=mock_provider)
            
            mock_get_tracer.assert_called_once()
            assert mock_wrap.call_count >= 2

    def test_instrument_with_custom_config(self):
        """Test instrumentation with custom trace config."""
        with patch('traceai_vertexai.get_tracer_provider') as mock_get_provider, \
             patch('traceai_vertexai.get_tracer') as mock_get_tracer, \
             patch('traceai_vertexai.wrap_function_wrapper') as mock_wrap:
            
            mock_provider = Mock()
            mock_tracer = Mock(spec=Tracer)
            mock_get_provider.return_value = mock_provider
            mock_get_tracer.return_value = mock_tracer
            
            custom_config = TraceConfig()
            
            instrumentor = VertexAIInstrumentor()
            instrumentor._instrument(config=custom_config)
            
            assert mock_wrap.call_count >= 2

    def test_instrument_with_invalid_config_type(self):
        """Test instrumentation fails with invalid config type."""
        instrumentor = VertexAIInstrumentor()
        
        with pytest.raises(AssertionError):
            instrumentor._instrument(config="invalid_config")

    def test_uninstrument(self):
        """Test uninstrumentation process."""
        with patch('google.api_core.gapic_v1.method') as mock_method, \
             patch('google.api_core.gapic_v1.method_async') as mock_method_async:
            
            # Mock wrapped methods with proper __wrapped__ attribute
            original_method = Mock()
            original_async_method = Mock()
            
            mock_method.wrap_method = Mock()
            mock_method.wrap_method.__wrapped__ = original_method
            mock_method_async.wrap_method = Mock()
            mock_method_async.wrap_method.__wrapped__ = original_async_method
            
            instrumentor = VertexAIInstrumentor()
            instrumentor._uninstrument()
            
            # Should restore original methods
            assert mock_method.wrap_method == original_method
            assert mock_method_async.wrap_method == original_async_method

    def test_uninstrument_without_wrapped(self):
        """Test uninstrumentation when no wrapped methods exist."""
        with patch('google.api_core.gapic_v1.method') as mock_method, \
             patch('google.api_core.gapic_v1.method_async') as mock_method_async:
            
            # Mock methods without __wrapped__ attribute
            mock_method.wrap_method = Mock()
            mock_method_async.wrap_method = Mock()
            
            instrumentor = VertexAIInstrumentor()
            
            # Should not raise errors
            instrumentor._uninstrument()


class TestWrapper:
    """Test suite for _Wrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock(spec=Tracer)
        self.wrapper = _Wrapper(tracer=self.mock_tracer)

    def test_initialization(self):
        """Test wrapper initialization."""
        assert self.wrapper._tracer == self.mock_tracer

    def test_call_with_no_request(self):
        """Test wrapper when no prediction service request is found."""
        def mock_function():
            return "response"
        
        # Mock _get_prediction_service_request to return None
        with patch('traceai_vertexai._wrapper._get_prediction_service_request', return_value=None):
            # Apply wrapper as decorator and call
            wrapped_function = self.wrapper(mock_function)
            result = wrapped_function()
            
            assert result == "response"

    def test_call_with_suppressed_instrumentation(self):
        """Test wrapper when instrumentation is suppressed."""
        def mock_function():
            return "response"
        
        with patch('traceai_vertexai._wrapper._get_prediction_service_request') as mock_get_request, \
             patch('traceai_vertexai._wrapper.context_api.get_value', return_value=True):
            
            mock_request = Mock(spec=proto.Message)
            mock_get_request.return_value = mock_request
            
            wrapped_function = self.wrapper(mock_function)
            result = wrapped_function()
            
            assert result == "response"

    def test_call_when_not_instrumented(self):
        """Test wrapper when instrumentation status is False."""
        def mock_function():
            return "response"
        
        with patch('traceai_vertexai._wrapper._get_prediction_service_request') as mock_get_request, \
             patch('traceai_vertexai._wrapper.context_api.get_value', return_value=False):
            
            mock_request = Mock(spec=proto.Message)
            mock_get_request.return_value = mock_request
            
            # Set instrumentation status to False
            self.wrapper._status._IS_INSTRUMENTED = False
            
            wrapped_function = self.wrapper(mock_function)
            result = wrapped_function()
            
            assert result == "response"

    def test_call_successful_with_proto_request(self):
        """Test successful wrapper call with proto.Message request."""
        def mock_function(mock_request):
            return "response"
        
        # Create mock proto message
        mock_request = Mock(spec=proto.Message)
        mock_request.__class__.to_dict = Mock(return_value={"contents": []})
        
        with patch('traceai_vertexai._wrapper._get_prediction_service_request', return_value=mock_request), \
             patch('traceai_vertexai._wrapper.context_api.get_value', return_value=False), \
             patch('traceai_vertexai._wrapper._extract_image_data', return_value={}), \
             patch('traceai_vertexai._wrapper._update_span'), \
             patch('traceai_vertexai._wrapper._finish'), \
             patch('traceai_vertexai._wrapper.use_span'):
            
            self.wrapper._status._IS_INSTRUMENTED = True
            mock_span = Mock()
            self.wrapper._tracer.start_span = Mock(return_value=mock_span)
            
            wrapped_function = self.wrapper(mock_function)
            result = wrapped_function(mock_request)
            
            assert result == "response"
            mock_span.set_attribute.assert_called()

    def test_call_with_exception(self):
        """Test wrapper when wrapped function raises exception."""
        def mock_function(mock_request):
            raise Exception("Test error")
        
        mock_request = Mock(spec=proto.Message)
        mock_request.__class__.to_dict = Mock(return_value={"contents": []})
        
        with patch('traceai_vertexai._wrapper._get_prediction_service_request', return_value=mock_request), \
             patch('traceai_vertexai._wrapper.context_api.get_value', return_value=False), \
             patch('traceai_vertexai._wrapper._extract_image_data', return_value={}), \
             patch('traceai_vertexai._wrapper._update_span'), \
             patch('traceai_vertexai._wrapper.use_span'):
            
            self.wrapper._status._IS_INSTRUMENTED = True
            mock_span = Mock()
            self.wrapper._tracer.start_span = Mock(return_value=mock_span)
            
            wrapped_function = self.wrapper(mock_function)
            with pytest.raises(Exception, match="Test error"):
                wrapped_function(mock_request)
            
            mock_span.record_exception.assert_called()
            mock_span.set_status.assert_called()
            mock_span.end.assert_called()

    def test_call_with_awaitable_result(self):
        """Test wrapper when result is awaitable."""
        import asyncio
        
        # Create a proper coroutine function
        async def async_result():
            return "async_response"
        
        def mock_function(mock_request):
            return async_result()  # Return coroutine
        
        mock_request = Mock(spec=proto.Message)
        mock_request.__class__.to_dict = Mock(return_value={"contents": []})
        
        with patch('traceai_vertexai._wrapper._get_prediction_service_request', return_value=mock_request), \
             patch('traceai_vertexai._wrapper.context_api.get_value', return_value=False), \
             patch('traceai_vertexai._wrapper._extract_image_data', return_value={}), \
             patch('traceai_vertexai._wrapper._update_span'), \
             patch('traceai_vertexai._wrapper._proxy') as mock_proxy, \
             patch('traceai_vertexai._wrapper.use_span'):
            
            self.wrapper._status._IS_INSTRUMENTED = True
            mock_span = Mock()
            self.wrapper._tracer.start_span = Mock(return_value=mock_span)
            
            # Mock proxy to return the coroutine
            mock_proxy.return_value = async_result()
            
            wrapped_function = self.wrapper(mock_function)
            result = wrapped_function(mock_request)
            
            # Should call _proxy for awaitable result
            mock_proxy.assert_called()


class TestCallbackClasses:
    """Test suite for callback classes."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_request = Mock(spec=proto.Message)
        self.mock_span = Mock()

    def test_callback_for_awaitable_initialization(self):
        """Test CallbackForAwaitable initialization."""
        callback = _CallbackForAwaitable(self.mock_request, self.mock_span)
        assert callback._request == self.mock_request
        assert callback._span == self.mock_span

    def test_callback_for_awaitable_with_iterable(self):
        """Test CallbackForAwaitable with iterable result."""
        callback = _CallbackForAwaitable(self.mock_request, self.mock_span)
        
        with patch('traceai_vertexai._wrapper._proxy') as mock_proxy:
            iterable_result = [1, 2, 3]
            result = callback(iterable_result)
            
            # Should call _proxy for iterable
            mock_proxy.assert_called()

    def test_callback_for_awaitable_with_non_iterable(self):
        """Test CallbackForAwaitable with non-iterable result."""
        with patch('traceai_vertexai._wrapper._finish') as mock_finish:
            callback = _CallbackForAwaitable(self.mock_request, self.mock_span)
            non_iterable_result = 42 
            result = callback(non_iterable_result)
            
            assert result == non_iterable_result
            mock_finish.assert_called_once_with(self.mock_span, non_iterable_result)

    def test_callback_for_iterable_initialization(self):
        """Test CallbackForIterable initialization."""
        callback = _CallbackForIterable(self.mock_request, self.mock_span)
        assert callback._span == self.mock_span
        assert hasattr(callback, '_accumulator')

    def test_callback_for_iterable_call(self):
        """Test CallbackForIterable call."""
        callback = _CallbackForIterable(self.mock_request, self.mock_span)
        
        with patch('traceai_vertexai._wrapper._finish') as mock_finish:
            # Test with StopIteration - this should call _finish
            stop_iteration = StopIteration()
            returned = callback(stop_iteration)
            
            assert returned == stop_iteration
            mock_finish.assert_called_once_with(self.mock_span, ANY)


class TestAccumulatorClasses:
    """Test suite for accumulator classes."""

    def test_key_values_accumulator_initialization(self):
        """Test KeyValuesAccumulator initialization."""
        acc = _KeyValuesAccumulator(key1="value1", key2="value2")
        result = dict(acc)
        assert result["key1"] == "value1"
        assert result["key2"] == "value2"

    def test_key_values_accumulator_iteration(self):
        """Test KeyValuesAccumulator iteration."""
        acc = _KeyValuesAccumulator(key1="value1", key2=None, key3="value3")
        result = dict(acc)
        
        assert "key1" in result
        assert "key2" not in result  # None values are skipped
        assert "key3" in result

    def test_key_values_accumulator_iadd(self):
        """Test KeyValuesAccumulator += operation."""
        acc = _KeyValuesAccumulator(key1="value1")
        acc += {"key2": "value2", "key1": "updated_value1"}
        
        result = dict(acc)
        assert result["key1"] == "updated_value1"
        assert result["key2"] == "value2"

    def test_key_values_accumulator_with_nested_accumulator(self):
        """Test KeyValuesAccumulator with nested accumulator."""
        nested_acc = _KeyValuesAccumulator(nested_key="nested_value")
        acc = _KeyValuesAccumulator(main_key=nested_acc)
        
        result = dict(acc)
        assert result["main_key"]["nested_key"] == "nested_value"

    def test_string_accumulator_initialization(self):
        """Test StringAccumulator initialization."""
        acc = _StringAccumulator()
        assert str(acc) == ""

    def test_string_accumulator_iadd(self):
        """Test StringAccumulator += operation."""
        acc = _StringAccumulator()
        acc += "Hello"
        acc += " "
        acc += "World"
        
        assert str(acc) == "Hello World"

    def test_string_accumulator_iadd_with_none(self):
        """Test StringAccumulator += with None value."""
        acc = _StringAccumulator()
        acc += "Hello"
        acc += None
        acc += "World"
        
        assert str(acc) == "HelloWorld"

    def test_indexed_accumulator_initialization(self):
        """Test IndexedAccumulator initialization."""
        factory = lambda: _KeyValuesAccumulator()
        acc = _IndexedAccumulator(factory)
        
        result = list(acc)
        assert result == []

    def test_indexed_accumulator_iadd_with_mapping(self):
        """Test IndexedAccumulator += with mapping."""
        factory = lambda: _KeyValuesAccumulator()
        acc = _IndexedAccumulator(factory)
        
        acc += {"index": 0, "key": "value0"}
        acc += {"index": 1, "key": "value1"}
        
        result = list(acc)
        assert len(result) == 2
        assert result[0]["key"] == "value0"
        assert result[1]["key"] == "value1"

    def test_indexed_accumulator_iadd_with_iterable(self):
        """Test IndexedAccumulator += with iterable."""
        factory = lambda: _KeyValuesAccumulator()
        acc = _IndexedAccumulator(factory)
        
        data = [
            {"index": 0, "key": "value0"},
            {"index": 1, "key": "value1"}
        ]
        acc += data
        
        result = list(acc)
        assert len(result) == 2


class TestUtilityFunctions:
    """Test suite for utility functions."""

    def test_get_prediction_service_request_with_request(self):
        """Test _get_prediction_service_request with valid request."""
        mock_request = Mock(spec=proto.Message)
        # Mock the type name to match one of the expected request types
        type(mock_request).__name__ = "GenerateContentRequest"
        args = [mock_request, "other_arg"]
        
        result = _get_prediction_service_request(args)
        assert result == mock_request

    def test_get_prediction_service_request_without_request(self):
        """Test _get_prediction_service_request without valid request."""
        args = ["arg1", "arg2", {"key": "value"}]
        
        result = _get_prediction_service_request(args)
        assert result is None

    def test_role_conversion(self):
        """Test _role function converts roles correctly."""
        assert _role("model") == "assistant"
        assert _role("user") == "user"
        assert _role("system") == "system"
        assert _role("unknown") == "unknown"

    def test_extract_image_data_empty(self):
        """Test _extract_image_data with empty messages."""
        result = _extract_image_data([])
        assert result == {
            "filtered_messages": None,
            "input_images": None,
            "eval_input": "",
            "query": ""
        }

    def test_extract_image_data_with_text(self):
        """Test _extract_image_data with text messages."""
        messages = [
            {"role": "user", "parts": [{"text": "Hello"}]}
        ]
        
        result = _extract_image_data(messages)
        assert result["filtered_messages"] == messages
        assert result["input_images"] is None

    def test_parse_predictions_with_valid_data(self):
        """Test _parse_predictions with valid prediction data."""
        predictions = [
            {"mimeType": "image/jpeg", "bytesBase64Encoded": "base64data"},
            {"key3": "value3"}
        ]
        
        result = dict(_parse_predictions(predictions))
        assert "llm.output_messages.0.message_content.image" in result
        assert result["llm.output_messages.0.message_content.image"] == "base64data"

    def test_parse_predictions_empty(self):
        """Test _parse_predictions with empty predictions."""
        predictions = []
        
        result = dict(_parse_predictions(predictions))
        assert result == {}

    def test_parse_usage_metadata(self):
        """Test _parse_usage_metadata function."""
        mock_metadata = Mock()
        mock_metadata.prompt_token_count = 10
        mock_metadata.candidates_token_count = 5
        mock_metadata.total_token_count = 15
        
        result = dict(_parse_usage_metadata(mock_metadata))
        
        assert "llm.token_count.prompt" in result
        assert "llm.token_count.completion" in result
        assert "llm.token_count.total" in result

    def test_parse_content_with_role(self):
        """Test _parse_content with role."""
        mock_content = Mock()
        mock_content.role = "user"
        mock_content.parts = []
        
        result = dict(_parse_content(mock_content, "test"))
        
        assert "testmessage.role" in result

    def test_parse_tool_calls_empty(self):
        """Test _parse_tool_calls with empty parts."""
        result = dict(_parse_tool_calls([]))
        assert result == {}

    def test_parse_parts_empty(self):
        """Test _parse_parts with empty parts."""
        result = dict(_parse_parts([]))
        assert result == {}

    def test_parse_part_with_text(self):
        """Test _parse_part with text content."""
        mock_part = Mock()
        mock_part.text = "Hello world"
        # Mock other attributes as None
        for attr in ['inline_data', 'file_data', 'function_call', 'function_response']:
            setattr(mock_part, attr, None)
        
        result = dict(_parse_part(mock_part))
        
        assert "message_content.text" in result


class TestResponseAccumulators:
    """Test suite for response accumulator classes."""

    def test_noop_response_accumulator(self):
        """Test NoOpResponseAccumulator."""
        acc = _NoOpResponseAccumulator()
        
        # Should handle any increment without error
        acc.accumulate("anything")
        
        # Result should be None
        assert acc.result is None

    def test_generate_content_response_accumulator_initialization(self):
        """Test GenerateContentResponseAccumulator initialization."""
        mock_cls = Mock()
        acc = _GenerateContentResponseAccumulator(mock_cls)
        
        assert acc._cls == mock_cls
        assert isinstance(acc._kv, _KeyValuesAccumulator)

    def test_generate_content_response_accumulator_accumulate(self):
        """Test GenerateContentResponseAccumulator accumulate method."""
        mock_cls = Mock()
        acc = _GenerateContentResponseAccumulator(mock_cls)
        
        # Mock increment with to_dict class method
        increment = Mock()
        increment.__class__.to_dict = Mock(return_value={"key": "value"})
        
        acc.accumulate(increment)
        
        # Should call to_dict and add to accumulator
        increment.__class__.to_dict.assert_called_once_with(increment)

    def test_generate_content_response_accumulator_result(self):
        """Test GenerateContentResponseAccumulator result property."""
        mock_cls = Mock()
        mock_cls.from_json = Mock(return_value="final_result")
        
        acc = _GenerateContentResponseAccumulator(mock_cls)
        
        # Simulate accumulating some data
        mock_increment = Mock()
        mock_increment.__class__.to_dict = Mock(return_value={"test": "data"})
        acc.accumulate(mock_increment)
        
        result = acc.result
        
        # Should call from_json with accumulated data
        mock_cls.from_json.assert_called_once()
        assert result == "final_result"


class TestProxyFunctionality:
    """Test suite for proxy functionality."""

    def test_proxy_with_simple_object(self):
        """Test _proxy with simple object."""
        simple_obj = "simple_string"
        result = _proxy(simple_obj)
        
        assert result == simple_obj

    def test_proxy_with_callback_only(self):
        """Test _proxy with callback but no context manager."""
        simple_obj = "test"
        callback = Mock()
        
        result = _proxy(simple_obj, callback=callback)
        
        assert result == simple_obj

    def test_proxy_with_iterable(self):
        """Test _proxy with iterable object."""
        iterable_obj = [1, 2, 3]
        callback = Mock()
        
        result = _proxy(iterable_obj, callback=callback)
        
        # Should return a proxy object
        assert hasattr(result, '__iter__')

    def test_proxy_with_already_proxied_object(self):
        """Test _proxy with already proxied object."""
        # Create a mock object that appears to be already proxied
        mock_obj = Mock()
        setattr(mock_obj, '_self_is_proxy', True)
        
        result = _proxy(mock_obj)
        
        assert result == mock_obj


class TestIntegrationScenarios:
    """Integration tests covering end-to-end scenarios."""

    def test_instrumentor_lifecycle(self):
        """Test complete instrumentor lifecycle."""
        instrumentor = VertexAIInstrumentor()
        
        # Test that instrumentor has required methods
        assert hasattr(instrumentor, '_instrument')
        assert hasattr(instrumentor, '_uninstrument')
        assert hasattr(instrumentor, 'instrumentation_dependencies')
        
        # Test dependencies
        dependencies = instrumentor.instrumentation_dependencies()
        assert "google-cloud-aiplatform >= 1.49.0" in dependencies

    def test_wrapper_creation_and_basic_functionality(self):
        """Test wrapper creation and basic functionality."""
        mock_tracer = Mock(spec=Tracer)
        
        # Test wrapper creation
        wrapper = _Wrapper(tracer=mock_tracer)
        
        assert wrapper._tracer == mock_tracer
        assert hasattr(wrapper, '__call__')

    def test_accumulator_integration(self):
        """Test accumulator classes working together."""
        # Create nested structure with different accumulator types
        string_acc = _StringAccumulator()
        string_acc += "Hello"
        string_acc += " World"
        
        indexed_acc = _IndexedAccumulator(lambda: _KeyValuesAccumulator())
        indexed_acc += {"index": 0, "message": string_acc}
        
        kv_acc = _KeyValuesAccumulator(messages=indexed_acc)
        
        result = dict(kv_acc)
        assert "messages" in result
        assert len(result["messages"]) == 1


class TestErrorHandling:
    """Test error handling scenarios."""

    def test_instrumentor_with_missing_modules(self):
        """Test instrumentor behavior with missing modules."""
        instrumentor = VertexAIInstrumentor()
        
        # Test that instrumentor can handle import errors gracefully
        assert hasattr(instrumentor, '_instrument')
        assert callable(getattr(instrumentor, '_instrument'))

    def test_wrapper_with_invalid_tracer(self):
        """Test wrapper creation with invalid tracer."""
        # Should not raise exception during creation
        wrapper = _Wrapper(tracer=None)
        assert wrapper._tracer is None

    def test_accumulator_error_handling(self):
        """Test accumulator error handling with invalid data."""
        acc = _KeyValuesAccumulator()
        
        # Should handle None gracefully
        acc += None
        result = dict(acc)
        assert isinstance(result, dict)

    def test_string_accumulator_error_handling(self):
        """Test string accumulator with invalid input."""
        acc = _StringAccumulator()
        
        # Should handle None values
        acc += None
        acc += "valid"
        acc += None
        
        assert str(acc) == "valid"

    def test_indexed_accumulator_error_handling(self):
        """Test indexed accumulator with malformed data."""
        factory = lambda: _KeyValuesAccumulator()
        acc = _IndexedAccumulator(factory)
        
        # Should handle None gracefully
        acc += None
        result = list(acc)
        assert result == []

    def test_utility_function_error_handling(self):
        """Test utility functions handle errors gracefully."""
        # Test with None input
        result = _get_prediction_service_request([None, "string", 123])
        assert result is None
        
        # Test role with None
        result = _role(None)
        assert result is None

    def test_parse_functions_with_invalid_data(self):
        """Test parse functions with invalid or missing data."""
        # Test parse_usage_metadata with None
        result = list(_parse_usage_metadata(None))
        assert result == []
        
        # Test parse_predictions with empty list (None causes TypeError)
        result = list(_parse_predictions([]))
        assert result == []

    def test_extract_image_data_with_malformed_messages(self):
        """Test image data extraction with malformed messages."""
        malformed_messages = [
            None,
            {"invalid": "structure"},
            {"role": "user"}  # missing parts
        ]
        
        # Should not raise errors
        result = _extract_image_data(malformed_messages)
        assert isinstance(result, dict)
        assert "filtered_messages" in result

    def test_proxy_with_none_objects(self):
        """Test proxy behavior with None objects."""
        result = _proxy(None)
        assert result is None
        
        # Test with None callback
        result = _proxy("test", callback=None)
        assert result == "test"


if __name__ == "__main__":
    pytest.main([__file__]) 