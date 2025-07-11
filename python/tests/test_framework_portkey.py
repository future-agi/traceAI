"""
Comprehensive test suite for Portkey instrumentation framework.
Tests the PortkeyInstrumentor and related wrapper functionality.
"""

import logging
import unittest
from unittest.mock import Mock, patch, MagicMock, ANY, call
from typing import Any, Collection, Dict, List, Mapping
from enum import Enum

import pytest
from portkey_ai.api_resources.apis.chat_complete import Completions

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.trace import Tracer, Status, StatusCode
from opentelemetry.trace.span import Span as OtelSpan
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.util.types import AttributeValue

from traceai_portkey import PortkeyInstrumentor
from traceai_portkey._wrappers import _CompletionsWrapper, _WithTracer, _flatten, _parse_args
from traceai_portkey._request_attributes_extractor import _RequestAttributesExtractor
from traceai_portkey._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_portkey._utils import _io_value_and_type, _as_input_attributes, _as_output_attributes, _finish_tracing, _ValueAndType
from traceai_portkey._with_span import _WithSpan


class TestPortkeyInstrumentor:
    """Test suite for PortkeyInstrumentor class."""

    def test_inheritance(self):
        """Test that PortkeyInstrumentor inherits from BaseInstrumentor."""
        instrumentor = PortkeyInstrumentor()
        assert isinstance(instrumentor, BaseInstrumentor)

    def test_instrumentation_dependencies(self):
        """Test that instrumentor returns correct dependencies."""
        instrumentor = PortkeyInstrumentor()
        dependencies = instrumentor.instrumentation_dependencies()
        assert isinstance(dependencies, Collection)
        assert "portkey_ai >= 0.1.0" in dependencies

    @patch('traceai_portkey.trace_api.get_tracer_provider')
    @patch('traceai_portkey.trace_api.get_tracer')
    @patch('traceai_portkey.wrap_function_wrapper')
    def test_instrument_with_default_config(self, mock_wrap, mock_get_tracer, mock_get_provider):
        """Test instrumentation with default configuration."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_provider.return_value = mock_provider
        mock_get_tracer.return_value = mock_tracer

        instrumentor = PortkeyInstrumentor()
        instrumentor._original_completions_create = None
        instrumentor._instrument()

        mock_get_provider.assert_called_once()
        mock_get_tracer.assert_called_once()
        assert mock_wrap.call_count == 2  # Two wrap calls

    @patch('traceai_portkey.trace_api.get_tracer')
    @patch('traceai_portkey.wrap_function_wrapper')
    def test_instrument_with_custom_tracer_provider(self, mock_wrap, mock_get_tracer):
        """Test instrumentation with custom tracer provider."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_tracer.return_value = mock_tracer

        instrumentor = PortkeyInstrumentor()
        instrumentor._original_completions_create = None
        instrumentor._instrument(tracer_provider=mock_provider)

        mock_get_tracer.assert_called_once()
        assert mock_wrap.call_count == 2

    @patch('traceai_portkey.trace_api.get_tracer_provider')
    @patch('traceai_portkey.trace_api.get_tracer')
    @patch('traceai_portkey.wrap_function_wrapper')
    def test_instrument_with_custom_config(self, mock_wrap, mock_get_tracer, mock_get_provider):
        """Test instrumentation with custom trace config."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_provider.return_value = mock_provider
        mock_get_tracer.return_value = mock_tracer
        
        custom_config = TraceConfig()
        instrumentor = PortkeyInstrumentor()
        instrumentor._original_completions_create = None
        instrumentor._instrument(config=custom_config)

        assert mock_wrap.call_count == 2

    @patch('traceai_portkey.trace_api.get_tracer_provider')
    @patch('traceai_portkey.trace_api.get_tracer')
    @patch('traceai_portkey.wrap_function_wrapper')
    def test_instrument_with_invalid_config_type(self, mock_wrap, mock_get_tracer, mock_get_provider):
        """Test instrumentation fails with invalid config type."""
        mock_provider = Mock()
        mock_get_provider.return_value = mock_provider

        instrumentor = PortkeyInstrumentor()
        
        with pytest.raises(AssertionError):
            instrumentor._instrument(config="invalid_config")

    @patch('traceai_portkey.import_module')
    def test_uninstrument(self, mock_import_module):
        """Test uninstrumentation process."""
        mock_module = Mock()
        mock_import_module.return_value = mock_module
        
        instrumentor = PortkeyInstrumentor()
        original_create = Mock()
        instrumentor._original_completions_create = original_create
        
        instrumentor._uninstrument()
        
        mock_import_module.assert_called_once_with("portkey_ai.api_resources.apis.chat_complete")
        assert mock_module.Completions.create == original_create

    @patch('traceai_portkey.import_module')
    def test_uninstrument_with_none_original(self, mock_import_module):
        """Test uninstrumentation when original function is None."""
        mock_module = Mock()
        mock_import_module.return_value = mock_module
        
        instrumentor = PortkeyInstrumentor()
        instrumentor._original_completions_create = None
        
        instrumentor._uninstrument()
        
        mock_import_module.assert_called_once()
        # Should not set attribute when original is None


class TestCompletionsWrapper:
    """Test suite for _CompletionsWrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock(spec=Tracer)
        self.wrapper = _CompletionsWrapper(tracer=self.mock_tracer)

    def test_initialization(self):
        """Test wrapper initialization."""
        assert self.wrapper._tracer == self.mock_tracer
        assert isinstance(self.wrapper._request_extractor, _RequestAttributesExtractor)
        assert isinstance(self.wrapper._response_extractor, _ResponseAttributesExtractor)

    def test_inheritance(self):
        """Test that _CompletionsWrapper inherits from _WithTracer."""
        assert isinstance(self.wrapper, _WithTracer)

    @patch('traceai_portkey._wrappers.context_api.get_value')
    def test_call_with_suppressed_instrumentation(self, mock_get_value):
        """Test wrapper when instrumentation is suppressed."""
        mock_get_value.return_value = True
        wrapped = Mock(return_value="response")
        
        result = self.wrapper(wrapped, None, (), {})
        
        assert result == "response"
        wrapped.assert_called_once_with()

    @patch('traceai_portkey._wrappers.context_api.get_value')
    @patch('traceai_portkey._wrappers._parse_args')
    @patch('traceai_portkey._wrappers.signature')
    @patch('traceai_portkey._wrappers.get_attributes_from_context')
    def test_call_successful_completion(self, mock_get_context, mock_signature, 
                                      mock_parse_args, mock_get_value):
        """Test successful completion call."""
        mock_get_value.return_value = False
        mock_parse_args.return_value = {"model": "gpt-3.5-turbo"}
        mock_signature.return_value = Mock()
        mock_get_context.return_value = []
        
        wrapped = Mock(return_value="response")
        
        # Mock the span context manager
        mock_span = Mock()
        self.wrapper._start_as_current_span = Mock()
        self.wrapper._start_as_current_span.return_value.__enter__ = Mock(return_value=mock_span)
        self.wrapper._start_as_current_span.return_value.__exit__ = Mock(return_value=None)
        
        result = self.wrapper(wrapped, None, (), {"model": "gpt-3.5-turbo"})
        
        assert result == "response"
        wrapped.assert_called_once_with(model="gpt-3.5-turbo")

    @patch('traceai_portkey._wrappers.context_api.get_value')
    @patch('traceai_portkey._wrappers._parse_args')
    @patch('traceai_portkey._wrappers.signature')
    @patch('traceai_portkey._wrappers.get_attributes_from_context')
    def test_call_with_exception(self, mock_get_context, mock_signature, 
                                mock_parse_args, mock_get_value):
        """Test completion call that raises exception."""
        mock_get_value.return_value = False
        mock_parse_args.return_value = {"model": "gpt-3.5-turbo"}
        mock_signature.return_value = Mock()
        mock_get_context.return_value = []
        
        test_exception = Exception("Test error")
        wrapped = Mock(side_effect=test_exception)
        
        # Mock the span context manager
        mock_span = Mock()
        self.wrapper._start_as_current_span = Mock()
        self.wrapper._start_as_current_span.return_value.__enter__ = Mock(return_value=mock_span)
        self.wrapper._start_as_current_span.return_value.__exit__ = Mock(return_value=None)
        
        with pytest.raises(Exception, match="Test error"):
            self.wrapper(wrapped, None, (), {"model": "gpt-3.5-turbo"})
        
        mock_span.record_exception.assert_called_once_with(test_exception)
        mock_span.finish_tracing.assert_called_once()


class TestWithTracer:
    """Test suite for _WithTracer base class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock(spec=Tracer)
        self.with_tracer = _WithTracer(tracer=self.mock_tracer)

    def test_initialization(self):
        """Test base class initialization."""
        assert self.with_tracer._tracer == self.mock_tracer

    @patch('traceai_portkey._wrappers.trace_api.use_span')
    def test_start_as_current_span(self, mock_use_span):
        """Test span context manager creation."""
        mock_span = Mock(spec=OtelSpan)
        self.mock_tracer.start_span.return_value = mock_span
        mock_use_span.return_value.__enter__ = Mock(return_value=mock_span)
        mock_use_span.return_value.__exit__ = Mock(return_value=None)
        
        attributes = [("key1", "value1")]
        context_attributes = [("context_key", "context_value")]
        extra_attributes = [("extra_key", "extra_value")]
        
        with self.with_tracer._start_as_current_span(
            "test_span", attributes, context_attributes, extra_attributes
        ) as span:
            assert isinstance(span, _WithSpan)
        
        self.mock_tracer.start_span.assert_called_once_with(
            name="test_span", 
            attributes={"extra_key": "extra_value"}
        )

    @patch('traceai_portkey._wrappers.trace_api.use_span')
    @patch('traceai_portkey._wrappers.INVALID_SPAN')
    def test_start_as_current_span_with_exception(self, mock_invalid_span, mock_use_span):
        """Test span creation when tracer raises exception."""
        self.mock_tracer.start_span.side_effect = Exception("Span creation failed")
        mock_use_span.return_value.__enter__ = Mock(return_value=mock_invalid_span)
        mock_use_span.return_value.__exit__ = Mock(return_value=None)
        
        with self.with_tracer._start_as_current_span(
            "test_span", [], [], []
        ) as span:
            assert isinstance(span, _WithSpan)


class TestRequestAttributesExtractor:
    """Test suite for _RequestAttributesExtractor class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.extractor = _RequestAttributesExtractor()

    def test_get_attributes_from_request(self):
        """Test basic request attribute extraction."""
        request_params = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}]}
        
        attributes = list(self.extractor.get_attributes_from_request(request_params))
        
        # Should contain span kind
        assert any(key == "fi.span.kind" for key, value in attributes)

    def test_get_extra_attributes_from_request_basic(self):
        """Test extra attributes extraction from request."""
        request_params = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "max_tokens": 100
        }
        
        attributes = dict(self.extractor.get_extra_attributes_from_request(request_params))
        
        # Should contain invocation parameters
        assert "llm.invocation_parameters" in attributes

    def test_get_extra_attributes_with_tools(self):
        """Test extra attributes extraction with tools."""
        request_params = {
            "model": "gpt-3.5-turbo",
            "tools": [
                {"type": "function", "function": {"name": "test_tool"}},
                {"type": "function", "function": {"name": "test_tool2"}}
            ]
        }
        
        attributes = dict(self.extractor.get_extra_attributes_from_request(request_params))
        
        # Should contain tool schemas
        assert "llm.tools.0.tool.json_schema" in attributes
        assert "llm.tools.1.tool.json_schema" in attributes

    def test_get_extra_attributes_with_prompt_id(self):
        """Test extra attributes extraction with prompt ID."""
        request_params = {
            "model": "gpt-3.5-turbo",
            "prompt_id": "test_prompt_123"
        }
        
        attributes = dict(self.extractor.get_extra_attributes_from_request(request_params))
        
        assert "prompt.id" in attributes
        assert attributes["prompt.id"] == "test_prompt_123"

    def test_get_extra_attributes_with_variables(self):
        """Test extra attributes extraction with prompt variables."""
        request_params = {
            "model": "gpt-3.5-turbo",
            "variables": {"name": "Alice", "topic": "AI"}
        }
        
        attributes = dict(self.extractor.get_extra_attributes_from_request(request_params))
        
        assert "llm.prompt_template.variables" in attributes

    def test_get_extra_attributes_with_messages(self):
        """Test extra attributes extraction with input messages."""
        request_params = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"}
            ]
        }
        
        attributes = dict(self.extractor.get_extra_attributes_from_request(request_params))
        
        # Should contain message attributes in reverse order (last first)
        assert "llm.input_messages.1.message.role" in attributes
        assert "llm.input_messages.0.message.role" in attributes

    def test_get_attributes_from_message_param_basic(self):
        """Test message parameter attribute extraction."""
        message = {"role": "user", "content": "Hello", "name": "test_user"}
        
        attributes = dict(self.extractor._get_attributes_from_message_param(message))
        
        assert attributes["message.role"] == "user"
        assert attributes["message.content"] == "Hello"
        assert attributes["message.name"] == "test_user"

    def test_get_attributes_from_message_param_with_tool_calls(self):
        """Test message parameter extraction with tool calls."""
        message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_123",
                    "function": {"name": "test_func", "arguments": '{"arg": "value"}'}
                }
            ]
        }
        
        attributes = dict(self.extractor._get_attributes_from_message_param(message))
        
        assert "message.tool_calls.0.tool_call.id" in attributes
        assert "message.tool_calls.0.tool_call.function.name" in attributes
        assert "message.tool_calls.0.tool_call.function.arguments" in attributes

    def test_get_attributes_from_message_param_with_function_call(self):
        """Test message parameter extraction with deprecated function call."""
        message = {
            "role": "assistant",
            "function_call": {"name": "test_func", "arguments": '{"arg": "value"}'}
        }
        
        attributes = dict(self.extractor._get_attributes_from_message_param(message))
        
        assert "message.function_call_name" in attributes
        assert "message.function_call_arguments_json" in attributes

    def test_get_attributes_from_non_mapping_request(self):
        """Test attribute extraction from non-mapping request."""
        # Should handle gracefully when request is not a mapping
        attributes = list(self.extractor.get_extra_attributes_from_request("invalid"))
        assert attributes == []

    def test_get_attribute_with_dict(self):
        """Test get_attribute utility with dictionary."""
        from traceai_portkey._request_attributes_extractor import get_attribute
        
        obj = {"key": "value", "nested": {"inner": "data"}}
        assert get_attribute(obj, "key") == "value"
        assert get_attribute(obj, "missing", "default") == "default"

    def test_get_attribute_with_object(self):
        """Test get_attribute utility with object."""
        from traceai_portkey._request_attributes_extractor import get_attribute
        
        class TestObj:
            attr = "value"
        
        obj = TestObj()
        assert get_attribute(obj, "attr") == "value"
        assert get_attribute(obj, "missing", "default") == "default"


class TestResponseAttributesExtractor:
    """Test suite for _ResponseAttributesExtractor class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.extractor = _ResponseAttributesExtractor()

    def test_get_attributes(self):
        """Test basic response attribute extraction."""
        response = Mock()
        response.model_dump_json.return_value = '{"id": "test"}'
        
        attributes = list(self.extractor.get_attributes(response))
        
        # Should have output attributes
        assert len(attributes) > 0

    def test_get_extra_attributes(self):
        """Test extra attributes extraction from response."""
        response = Mock()
        response.model = "gpt-3.5-turbo"
        request_params = {"model": "gpt-3.5-turbo"}
        
        attributes = list(self.extractor.get_extra_attributes(response, request_params))
        
        # Should contain model name
        assert any("llm.model_name" in attr for attr, value in attributes)

    def test_get_attributes_from_chat_completion(self):
        """Test chat completion attribute extraction."""
        completion = Mock()
        completion.model = "gpt-3.5-turbo"
        completion.usage = Mock()
        completion.usage.total_tokens = 100
        completion.usage.prompt_tokens = 50
        completion.usage.completion_tokens = 50
        
        completion.choices = [Mock()]
        completion.choices[0].index = 0
        completion.choices[0].message = Mock()
        completion.choices[0].message.role = "assistant"
        completion.choices[0].message.content = "Hello!"
        
        request_params = {}
        
        attributes = dict(self.extractor._get_attributes_from_chat_completion(completion, request_params))
        
        assert attributes["llm.model_name"] == "gpt-3.5-turbo"
        assert attributes["llm.token_count.total"] == 100
        assert attributes["llm.token_count.prompt"] == 50
        assert attributes["llm.token_count.completion"] == 50

    def test_get_attributes_from_chat_completion_message(self):
        """Test chat completion message attribute extraction."""
        message = Mock()
        message.role = "assistant"
        message.content = "Hello there!"
        message.function_call = Mock()
        message.function_call.name = "test_func"
        message.function_call.arguments = '{"arg": "value"}'
        
        attributes = dict(self.extractor._get_attributes_from_chat_completion_message(message))
        
        assert attributes["message.role"] == "assistant"
        assert attributes["message.content"] == "Hello there!"
        assert attributes["message.function_call_name"] == "test_func"
        assert attributes["message.function_call_arguments_json"] == '{"arg": "value"}'

    def test_get_attributes_from_completion_usage(self):
        """Test completion usage attribute extraction."""
        usage = Mock()
        usage.total_tokens = 150
        usage.prompt_tokens = 75
        usage.completion_tokens = 75
        
        attributes = dict(self.extractor._get_attributes_from_completion_usage(usage))
        
        assert attributes["llm.token_count.total"] == 150
        assert attributes["llm.token_count.prompt"] == 75
        assert attributes["llm.token_count.completion"] == 75

    def test_get_attributes_from_completion_without_usage(self):
        """Test completion without usage information."""
        completion = Mock()
        completion.model = "gpt-3.5-turbo"
        completion.usage = None
        completion.choices = []
        
        request_params = {}
        
        attributes = dict(self.extractor._get_attributes_from_chat_completion(completion, request_params))
        
        assert attributes["llm.model_name"] == "gpt-3.5-turbo"
        # Should not have usage attributes
        assert "llm.token_count.total" not in attributes


class TestUtilityFunctions:
    """Test suite for utility functions."""

    def test_io_value_and_type_with_pydantic_model(self):
        """Test I/O value and type extraction with Pydantic model."""
        mock_obj = Mock()
        mock_obj.model_dump_json.return_value = '{"key": "value"}'
        
        result = _io_value_and_type(mock_obj)
        
        assert result.value == '{"key": "value"}'
        assert result.type.value == "application/json"

    def test_io_value_and_type_with_dict(self):
        """Test I/O value and type extraction with dictionary."""
        obj = {"key": "value", "nested": {"inner": "data"}}
        
        result = _io_value_and_type(obj)
        
        assert result.type.value == "application/json"
        assert "key" in result.value

    def test_io_value_and_type_with_string(self):
        """Test I/O value and type extraction with string."""
        obj = "test string"
        
        result = _io_value_and_type(obj)
        
        assert result.value == "test string"
        assert result.type.value == "text/plain"

    def test_as_input_attributes(self):
        """Test input attributes conversion."""
        from fi_instrumentation.fi_types import FiMimeTypeValues
        value_and_type = _ValueAndType("test", FiMimeTypeValues.JSON)
        
        attributes = dict(_as_input_attributes(value_and_type))
        
        assert "input.value" in attributes
        assert "input.mime_type" in attributes

    def test_as_output_attributes(self):
        """Test output attributes conversion."""
        from fi_instrumentation.fi_types import FiMimeTypeValues
        value_and_type = _ValueAndType("test", FiMimeTypeValues.JSON)
        
        attributes = dict(_as_output_attributes(value_and_type))
        
        assert "output.value" in attributes
        assert "output.mime_type" in attributes

    def test_finish_tracing(self):
        """Test finish tracing utility function."""
        mock_span = Mock()
        attributes = [("key1", "value1")]
        extra_attributes = [("key2", "value2")]
        
        _finish_tracing(mock_span, attributes, extra_attributes)
        
        mock_span.finish_tracing.assert_called_once()

    def test_finish_tracing_with_exception(self):
        """Test finish tracing with attribute extraction exception."""
        mock_span = Mock()
        
        # Create attributes that will raise exception when converted to dict
        class BadIterable:
            def __iter__(self):
                raise Exception("Test exception")
        
        with patch('traceai_portkey._utils.logger') as mock_logger:
            _finish_tracing(mock_span, BadIterable(), [])
            mock_logger.exception.assert_called()


class TestWithSpan:
    """Test suite for _WithSpan class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_span = Mock(spec=OtelSpan)
        self.mock_span.is_recording.return_value = True
        self.with_span = _WithSpan(self.mock_span)

    def test_initialization(self):
        """Test _WithSpan initialization."""
        assert self.with_span._span == self.mock_span
        assert not self.with_span.is_finished

    def test_initialization_with_non_recording_span(self):
        """Test initialization with non-recording span."""
        self.mock_span.is_recording.return_value = False
        with_span = _WithSpan(self.mock_span)
        assert with_span.is_finished

    def test_record_exception(self):
        """Test exception recording."""
        exception = Exception("test error")
        self.with_span.record_exception(exception)
        
        self.mock_span.record_exception.assert_called_once_with(exception)

    def test_record_exception_when_finished(self):
        """Test exception recording on finished span."""
        self.with_span._is_finished = True
        exception = Exception("test error")
        
        self.with_span.record_exception(exception)
        
        self.mock_span.record_exception.assert_not_called()

    def test_add_event(self):
        """Test adding event to span."""
        self.with_span.add_event("test_event")
        
        self.mock_span.add_event.assert_called_once_with("test_event")

    def test_set_attributes(self):
        """Test setting attributes on span."""
        attributes = {"key": "value"}
        self.with_span.set_attributes(attributes)
        
        self.mock_span.set_attributes.assert_called_once_with(attributes)

    def test_finish_tracing(self):
        """Test finishing tracing."""
        status = Mock()
        attributes = {"key1": "value1"}
        extra_attributes = {"key2": "value2"}
        
        self.with_span.finish_tracing(status, attributes, extra_attributes)
        
        self.mock_span.set_status.assert_called_once_with(status=status)
        self.mock_span.end.assert_called_once()
        assert self.with_span.is_finished

    def test_finish_tracing_when_already_finished(self):
        """Test finishing tracing when already finished."""
        self.with_span._is_finished = True
        
        self.with_span.finish_tracing()
        
        self.mock_span.set_status.assert_not_called()
        self.mock_span.end.assert_not_called()


class TestWrapperUtilities:
    """Test suite for wrapper utility functions."""

    def test_flatten_simple_mapping(self):
        """Test flattening simple mapping."""
        mapping = {"key1": "value1", "key2": "value2"}
        
        result = dict(_flatten(mapping))
        
        assert result["key1"] == "value1"
        assert result["key2"] == "value2"

    def test_flatten_nested_mapping(self):
        """Test flattening nested mapping."""
        mapping = {"outer": {"inner": "value"}}
        
        result = dict(_flatten(mapping))
        
        assert result["outer.inner"] == "value"

    def test_flatten_with_list_of_mappings(self):
        """Test flattening with list of mappings."""
        mapping = {"items": [{"name": "item1"}, {"name": "item2"}]}
        
        result = dict(_flatten(mapping))
        
        assert result["items.0.name"] == "item1"
        assert result["items.1.name"] == "item2"

    def test_flatten_with_enum(self):
        """Test flattening with enum values."""
        class TestEnum(Enum):
            VALUE = "test_value"
        
        mapping = {"enum_key": TestEnum.VALUE}
        
        result = dict(_flatten(mapping))
        
        assert result["enum_key"] == "test_value"

    def test_flatten_skips_none_values(self):
        """Test that flatten skips None values."""
        mapping = {"key1": "value1", "key2": None, "key3": "value3"}
        
        result = dict(_flatten(mapping))
        
        assert "key1" in result
        assert "key2" not in result
        assert "key3" in result

    def test_parse_args(self):
        """Test argument parsing utility."""
        from inspect import signature
        
        def test_func(arg1, arg2="default", *args, **kwargs):
            pass
        
        sig = signature(test_func)
        result = _parse_args(sig, "value1", arg2="value2", extra="extra_value")
        
        assert result["arg1"] == "value1"
        assert result["arg2"] == "value2"
        assert result["args"] == ()
        assert result["kwargs"] == {"extra": "extra_value"}


class TestIntegrationScenarios:
    """Integration tests covering end-to-end scenarios."""

    @patch('traceai_portkey.wrap_function_wrapper')
    @patch('traceai_portkey.trace_api.get_tracer_provider')
    @patch('traceai_portkey.trace_api.get_tracer')
    def test_full_instrumentation_lifecycle(self, mock_get_tracer, mock_get_provider, mock_wrap):
        """Test complete instrumentation and uninstrumentation."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_provider.return_value = mock_provider
        mock_get_tracer.return_value = mock_tracer

        instrumentor = PortkeyInstrumentor()
        instrumentor._original_completions_create = None
        
        # Test instrumentation
        instrumentor._instrument()
        
        assert mock_wrap.call_count == 2
        
        # Test uninstrumentation
        with patch('traceai_portkey.import_module') as mock_import:
            mock_module = Mock()
            mock_import.return_value = mock_module
            original_create = Mock()
            instrumentor._original_completions_create = original_create
            
            instrumentor._uninstrument()
            
            mock_import.assert_called_once()

    def test_wrapper_request_response_flow(self):
        """Test complete request-response flow through wrapper."""
        mock_tracer = Mock(spec=Tracer)
        wrapper = _CompletionsWrapper(tracer=mock_tracer)
        
        # Test that extractor objects are created
        assert isinstance(wrapper._request_extractor, _RequestAttributesExtractor)
        assert isinstance(wrapper._response_extractor, _ResponseAttributesExtractor)

    def test_attribute_extraction_pipeline(self):
        """Test complete attribute extraction pipeline."""
        # Test request extraction
        request_extractor = _RequestAttributesExtractor()
        request_params = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7
        }
        
        request_attrs = list(request_extractor.get_attributes_from_request(request_params))
        extra_attrs = list(request_extractor.get_extra_attributes_from_request(request_params))
        
        assert len(request_attrs) > 0
        assert len(extra_attrs) > 0
        
        # Test response extraction
        response_extractor = _ResponseAttributesExtractor()
        response = Mock()
        response.model = "gpt-3.5-turbo"
        response.usage = Mock()
        response.usage.total_tokens = 100
        
        response_attrs = list(response_extractor.get_attributes(response))
        extra_response_attrs = list(response_extractor.get_extra_attributes(response, request_params))
        
        assert len(response_attrs) > 0
        assert len(extra_response_attrs) > 0


class TestErrorHandling:
    """Test error handling scenarios."""

    def test_instrumentor_with_import_error(self):
        """Test instrumentation when portkey module is not available."""
        with patch('traceai_portkey.wrap_function_wrapper') as mock_wrap:
            mock_wrap.side_effect = ImportError("No module named 'portkey_ai'")
            
            instrumentor = PortkeyInstrumentor()
            
            with pytest.raises(ImportError):
                instrumentor._instrument()

    def test_wrapper_with_invalid_tracer(self):
        """Test wrapper creation with invalid tracer."""
        # Should not raise exception during creation
        wrapper = _CompletionsWrapper(tracer=None)
        assert wrapper._tracer is None

    def test_attribute_extraction_with_malformed_data(self):
        """Test attribute extractors handle malformed data gracefully."""
        extractor = _RequestAttributesExtractor()
        
        # Test with malformed request parameters
        malformed_params = {"messages": "not_a_list", "tools": "not_iterable"}
        
        # Should not raise exceptions
        attrs = list(extractor.get_attributes_from_request(malformed_params))
        extra_attrs = list(extractor.get_extra_attributes_from_request(malformed_params))
        
        assert isinstance(attrs, list)
        assert isinstance(extra_attrs, list)

    def test_span_creation_error_handling(self):
        """Test span creation error handling."""
        mock_span = Mock()
        mock_span.is_recording.side_effect = Exception("Span check failed")
        
        # Should handle span recording check failure gracefully
        with_span = _WithSpan(mock_span)
        assert with_span.is_finished  # Should default to finished on error

    def test_io_value_type_extraction_error_handling(self):
        """Test I/O value and type extraction error handling."""
        # Test with object that has model_dump_json but raises exception
        mock_obj = Mock()
        mock_obj.model_dump_json.side_effect = Exception("Serialization failed")
        
        result = _io_value_and_type(mock_obj)
        
        # Should fall back to string representation
        assert result.type.value == "text/plain"


if __name__ == "__main__":
    pytest.main([__file__]) 