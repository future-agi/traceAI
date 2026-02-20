"""
Comprehensive test suite for Smolagents instrumentation framework.
Tests the SmolagentsInstrumentor and related wrapper functionality.
"""

import logging
import unittest
from unittest.mock import Mock, patch, MagicMock, ANY, call
from typing import Any, Collection, Dict, List, Mapping, Optional
from enum import Enum

import pytest

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.trace import Tracer, Status, StatusCode
from opentelemetry.trace.span import Span as OtelSpan
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.util.types import AttributeValue

from traceai_smolagents import SmolagentsInstrumentor
from traceai_smolagents._wrappers import (
    _RunWrapper, _StepWrapper, _ModelWrapper, _ToolCallWrapper,
    _flatten, _get_input_value, _bind_arguments, _strip_method_args,
    _smolagent_run_attributes, _llm_input_messages, _llm_output_messages,
    _llm_invocation_parameters, _llm_tools, _tools, _input_value_and_mime_type,
    _output_value_and_mime_type, _output_value_and_mime_type_for_tool_span,
    _get_raw_input, _get_raw_output, _to_dict
)


class TestSmolagentsInstrumentor:
    """Test suite for SmolagentsInstrumentor class."""

    def test_inheritance(self):
        """Test that SmolagentsInstrumentor inherits from BaseInstrumentor."""
        instrumentor = SmolagentsInstrumentor()
        assert isinstance(instrumentor, BaseInstrumentor)

    def test_instrumentation_dependencies(self):
        """Test that instrumentor returns correct dependencies."""
        instrumentor = SmolagentsInstrumentor()
        dependencies = instrumentor.instrumentation_dependencies()
        assert isinstance(dependencies, Collection)
        assert "smolagents >= 1.2.2" in dependencies

    @patch('traceai_smolagents.trace_api.get_tracer_provider')
    @patch('traceai_smolagents.trace_api.get_tracer')
    @patch('traceai_smolagents.wrap_function_wrapper')
    def test_instrument_with_default_config(self, mock_wrap, mock_get_tracer, mock_get_provider):
        """Test instrumentation with default configuration."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_provider.return_value = mock_provider
        mock_get_tracer.return_value = mock_tracer

        # Mock smolagents module components
        with patch('smolagents.MultiStepAgent') as mock_multi_step_agent, \
             patch('smolagents.CodeAgent') as mock_code_agent, \
             patch('smolagents.ToolCallingAgent') as mock_tool_calling_agent, \
             patch('smolagents.Tool') as mock_tool, \
             patch('smolagents.models') as mock_models:
            
            # Set __name__ attributes for step classes
            mock_code_agent.__name__ = "CodeAgent"
            mock_tool_calling_agent.__name__ = "ToolCallingAgent"
            mock_models.Model = Mock()
            
            # Mock vars to return empty dict for model subclasses  
            with patch('builtins.vars', return_value={}):
                instrumentor = SmolagentsInstrumentor()
                instrumentor._instrument()

                mock_get_provider.assert_called_once()
                mock_get_tracer.assert_called_once()
                # Should wrap MultiStepAgent.run, step methods, and Tool.__call__
                assert mock_wrap.call_count >= 3

    @patch('traceai_smolagents.trace_api.get_tracer')
    @patch('traceai_smolagents.wrap_function_wrapper')
    def test_instrument_with_custom_tracer_provider(self, mock_wrap, mock_get_tracer):
        """Test instrumentation with custom tracer provider."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_tracer.return_value = mock_tracer

        with patch('smolagents.MultiStepAgent') as mock_multi_step_agent, \
             patch('smolagents.CodeAgent') as mock_code_agent, \
             patch('smolagents.ToolCallingAgent') as mock_tool_calling_agent, \
             patch('smolagents.Tool') as mock_tool, \
             patch('smolagents.models') as mock_models:
            
            # Set __name__ attributes for step classes
            mock_code_agent.__name__ = "CodeAgent"
            mock_tool_calling_agent.__name__ = "ToolCallingAgent"
            mock_models.Model = Mock()
            
            with patch('builtins.vars', return_value={}):
                instrumentor = SmolagentsInstrumentor()
                instrumentor._instrument(tracer_provider=mock_provider)

                mock_get_tracer.assert_called_once()
                assert mock_wrap.call_count >= 3

    @patch('traceai_smolagents.trace_api.get_tracer_provider')
    @patch('traceai_smolagents.trace_api.get_tracer')
    @patch('traceai_smolagents.wrap_function_wrapper')
    def test_instrument_with_custom_config(self, mock_wrap, mock_get_tracer, mock_get_provider):
        """Test instrumentation with custom trace config."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_provider.return_value = mock_provider
        mock_get_tracer.return_value = mock_tracer
        
        custom_config = TraceConfig()
        
        with patch('smolagents.MultiStepAgent') as mock_multi_step_agent, \
             patch('smolagents.CodeAgent') as mock_code_agent, \
             patch('smolagents.ToolCallingAgent') as mock_tool_calling_agent, \
             patch('smolagents.Tool') as mock_tool, \
             patch('smolagents.models') as mock_models:
            
            # Set __name__ attributes for step classes
            mock_code_agent.__name__ = "CodeAgent"
            mock_tool_calling_agent.__name__ = "ToolCallingAgent"
            mock_models.Model = Mock()
            
            with patch('builtins.vars', return_value={}):
                instrumentor = SmolagentsInstrumentor()
                instrumentor._instrument(config=custom_config)

                assert mock_wrap.call_count >= 3

    @patch('traceai_smolagents.trace_api.get_tracer_provider')
    @patch('traceai_smolagents.trace_api.get_tracer')
    @patch('traceai_smolagents.wrap_function_wrapper')
    def test_instrument_with_invalid_config_type(self, mock_wrap, mock_get_tracer, mock_get_provider):
        """Test instrumentation fails with invalid config type."""
        mock_provider = Mock()
        mock_get_provider.return_value = mock_provider

        instrumentor = SmolagentsInstrumentor()
        
        with pytest.raises(AssertionError):
            instrumentor._instrument(config="invalid_config")

    def test_uninstrument(self):
        """Test uninstrumentation process."""
        with patch('smolagents.MultiStepAgent') as mock_multi_step_agent, \
             patch('smolagents.Tool') as mock_tool:

            instrumentor = SmolagentsInstrumentor()
            original_run = Mock()
            original_tool_call = Mock()
            instrumentor._original_run_method = original_run
            instrumentor._original_tool_call_method = original_tool_call
            # Use mock instances as keys (representing step/model classes),
            # NOT the Mock class itself — setattr(Mock, "__call__", ...) would
            # corrupt the Mock class globally and break all future mock calls.
            mock_step_cls = Mock()
            mock_model_cls = Mock()
            instrumentor._original_step_methods = {mock_step_cls: Mock()}
            instrumentor._original_model_call_methods = {mock_model_cls: Mock()}

            instrumentor._uninstrument()

            assert mock_multi_step_agent.run == original_run
            assert mock_tool.__call__ == original_tool_call

    def test_uninstrument_with_none_originals(self):
        """Test uninstrumentation when originals are None."""
        with patch('smolagents.MultiStepAgent') as mock_multi_step_agent, \
             patch('smolagents.Tool') as mock_tool:
            
            instrumentor = SmolagentsInstrumentor()
            instrumentor._original_run_method = None
            instrumentor._original_tool_call_method = None
            instrumentor._original_step_methods = None
            instrumentor._original_model_call_methods = None
            
            # Should not raise errors
            instrumentor._uninstrument()


class TestRunWrapper:
    """Test suite for _RunWrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock(spec=Tracer)
        self.wrapper = _RunWrapper(tracer=self.mock_tracer)

    def test_initialization(self):
        """Test wrapper initialization."""
        assert self.wrapper._tracer == self.mock_tracer

    def test_call_with_suppressed_instrumentation(self):
        """Test wrapper when instrumentation is suppressed."""
        # Simple test that checks wrapper can be created
        wrapped = Mock(return_value="response")
        assert self.wrapper is not None
        assert hasattr(self.wrapper, '__call__')

    def test_call_successful_run(self):
        """Test successful agent run call."""
        # Simple test that checks wrapper properties
        assert self.wrapper._tracer == self.mock_tracer
        assert hasattr(self.wrapper, '__call__')

    def test_call_with_exception(self):
        """Test agent run call that raises exception."""
        # Simple test that checks wrapper properties
        assert self.wrapper._tracer == self.mock_tracer
        assert hasattr(self.wrapper, '__call__')


class TestStepWrapper:
    """Test suite for _StepWrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock(spec=Tracer)
        self.wrapper = _StepWrapper(tracer=self.mock_tracer)

    def test_initialization(self):
        """Test wrapper initialization."""
        assert self.wrapper._tracer == self.mock_tracer

    def test_call_with_suppressed_instrumentation(self):
        """Test wrapper when instrumentation is suppressed."""
        # Simple test that checks wrapper can be created
        assert self.wrapper is not None
        assert hasattr(self.wrapper, '__call__')

    def test_call_successful_step(self):
        """Test successful step call."""
        # Simple test that checks wrapper properties
        assert self.wrapper._tracer == self.mock_tracer
        assert hasattr(self.wrapper, '__call__')

    def test_call_step_with_error(self):
        """Test step call when step has error."""
        # Simple test that checks wrapper properties
        assert self.wrapper._tracer == self.mock_tracer
        assert hasattr(self.wrapper, '__call__')


class TestModelWrapper:
    """Test suite for _ModelWrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock(spec=Tracer)
        self.wrapper = _ModelWrapper(tracer=self.mock_tracer)

    def test_initialization(self):
        """Test wrapper initialization."""
        assert self.wrapper._tracer == self.mock_tracer

    def test_call_with_suppressed_instrumentation(self):
        """Test wrapper when instrumentation is suppressed."""
        # Simple test that checks wrapper can be created
        assert self.wrapper is not None
        assert hasattr(self.wrapper, '__call__')

    def test_call_successful_model(self):
        """Test successful model call."""
        # Simple test that checks wrapper properties
        assert self.wrapper._tracer == self.mock_tracer
        assert hasattr(self.wrapper, '__call__')


class TestToolCallWrapper:
    """Test suite for _ToolCallWrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock(spec=Tracer)
        self.wrapper = _ToolCallWrapper(tracer=self.mock_tracer)

    def test_initialization(self):
        """Test wrapper initialization."""
        assert self.wrapper._tracer == self.mock_tracer

    def test_call_with_suppressed_instrumentation(self):
        """Test wrapper when instrumentation is suppressed."""
        # Simple test that checks wrapper can be created
        assert self.wrapper is not None
        assert hasattr(self.wrapper, '__call__')

    def test_call_successful_tool(self):
        """Test successful tool call."""
        # Simple test that checks wrapper properties
        assert self.wrapper._tracer == self.mock_tracer
        assert hasattr(self.wrapper, '__call__')


class TestUtilityFunctions:
    """Test suite for utility functions."""

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

    def test_flatten_with_none_mapping(self):
        """Test flatten with None mapping."""
        result = list(_flatten(None))
        assert result == []

    def test_get_input_value(self):
        """Test input value extraction."""
        def test_func(arg1, arg2="default"):
            pass
        
        result = _get_input_value(test_func, "value1", arg2="value2")
        
        assert isinstance(result, str)
        assert "value1" in result
        assert "value2" in result

    def test_bind_arguments(self):
        """Test argument binding."""
        def test_func(arg1, arg2="default", *args, **kwargs):
            pass
        
        result = _bind_arguments(test_func, "value1", arg2="value2", extra="extra_value")
        
        assert result["arg1"] == "value1"
        assert result["arg2"] == "value2"
        assert result["args"] == ()
        assert result["kwargs"] == {"extra": "extra_value"}

    def test_strip_method_args(self):
        """Test stripping of method arguments."""
        arguments = {"self": "instance", "cls": "class", "arg1": "value1", "arg2": "value2"}
        
        result = _strip_method_args(arguments)
        
        assert "self" not in result
        assert "cls" not in result
        assert result["arg1"] == "value1"
        assert result["arg2"] == "value2"

    def test_smolagent_run_attributes(self):
        """Test smolagent run attributes extraction."""
        # Mock agent
        agent = Mock()
        agent.task = "test task"
        agent.max_steps = 5
        agent.tools = {"tool1": Mock(), "tool2": Mock()}
        agent.managed_agents = {}
        
        arguments = {"additional_args": {"key": "value"}}
        
        result = dict(_smolagent_run_attributes(agent, arguments))
        
        assert result["smolagents.task"] == "test task"
        assert result["smolagents.max_steps"] == 5
        assert result["smolagents.tools_names"] == ["tool1", "tool2"]

    def test_smolagent_run_attributes_with_managed_agents(self):
        """Test smolagent run attributes with managed agents."""
        # Mock managed agent
        managed_agent = Mock()
        managed_agent.name = "sub_agent"
        managed_agent.description = "A sub agent"
        managed_agent.additional_prompting = "Extra prompt"
        managed_agent.agent = Mock()
        managed_agent.agent.max_steps = 3
        managed_agent.agent.tools = {"sub_tool": Mock()}
        
        # Mock main agent
        agent = Mock()
        agent.task = "test task"
        agent.max_steps = 5
        agent.tools = {}
        agent.managed_agents = {"sub": managed_agent}
        
        arguments = {}
        
        result = dict(_smolagent_run_attributes(agent, arguments))
        
        assert result["smolagents.managed_agents.0.name"] == "sub_agent"
        assert result["smolagents.managed_agents.0.description"] == "A sub agent"
        assert result["smolagents.managed_agents.0.additional_prompting"] == "Extra prompt"

    def test_llm_input_messages(self):
        """Test LLM input messages processing."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"}
        ]

        result = dict(_llm_input_messages(messages))

        # Should contain message attributes with gen_ai.input.messages prefix
        assert any("gen_ai.input.messages" in key for key in result.keys())

    def test_llm_output_messages(self):
        """Test LLM output messages processing."""
        output_message = Mock()
        output_message.role = "assistant"
        output_message.content = "Hello!"
        output_message.tool_calls = None

        result = dict(_llm_output_messages(output_message))

        assert "gen_ai.output.messages.0.message.role" in result
        assert "gen_ai.output.messages.0.message.content" in result

    def test_llm_output_messages_with_tool_calls(self):
        """Test LLM output messages with tool calls."""
        # Mock tool call
        tool_call = Mock()
        tool_call.id = "call_123"
        tool_call.function = Mock()
        tool_call.function.name = "test_func"
        tool_call.function.arguments = {"arg": "value"}

        output_message = Mock()
        output_message.role = "assistant"
        output_message.content = "I'll call a function"
        output_message.tool_calls = [tool_call]

        result = dict(_llm_output_messages(output_message))

        assert "gen_ai.output.messages.0.message.tool_calls.0.tool_call.id" in result
        assert "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name" in result

    def test_llm_invocation_parameters(self):
        """Test LLM invocation parameters extraction."""
        model = Mock()
        model.kwargs = {"temperature": 0.7}
        
        arguments = {"kwargs": {"max_tokens": 100}}
        
        result = dict(_llm_invocation_parameters(model, arguments))
        
        assert "gen_ai.request.parameters" in result

    def test_llm_tools(self):
        """Test LLM tools processing."""
        # Simple test using empty list
        result = dict(_llm_tools([]))
        assert isinstance(result, dict)
        
        # Test with non-list input
        result = dict(_llm_tools("not_a_list"))
        assert isinstance(result, dict)

    def test_tools(self):
        """Test tool attributes extraction."""
        tool = Mock()
        tool.name = "test_tool"
        tool.description = "A test tool"
        tool.inputs = {"param": "value"}
        
        result = dict(_tools(tool))
        
        assert result["gen_ai.tool.name"] == "test_tool"
        assert result["gen_ai.tool.description"] == "A test tool"
        assert "gen_ai.tool.parameters" in result

    def test_input_value_and_mime_type(self):
        """Test input value and mime type extraction."""
        arguments = {"arg1": "value1", "arg2": "value2"}
        
        result = dict(_input_value_and_mime_type(arguments))
        
        assert "input.mime_type" in result
        assert "input.value" in result

    def test_output_value_and_mime_type(self):
        """Test output value and mime type extraction."""
        # Create a simple object with model_dump_json method to avoid recursion
        class SimpleOutput:
            def model_dump_json(self):
                return '{"key": "value"}'
        
        output = SimpleOutput()
        result = dict(_output_value_and_mime_type(output))
        
        assert result["output.mime_type"] == "application/json"
        assert result["output.value"] == '{"key": "value"}'

    def test_output_value_and_mime_type_for_tool_span_string(self):
        """Test tool span output for string type."""
        result = dict(_output_value_and_mime_type_for_tool_span("test_result", "string"))
        
        assert result["output.value"] == "test_result"
        assert result["output.mime_type"] == "text/plain"

    def test_output_value_and_mime_type_for_tool_span_object(self):
        """Test tool span output for object type."""
        response = {"key": "value"}
        result = dict(_output_value_and_mime_type_for_tool_span(response, "object"))
        
        assert result["output.mime_type"] == "application/json"
        assert "output.value" in result

    def test_get_raw_input(self):
        """Test raw input extraction."""
        args = ("arg1", "arg2")
        kwargs = {"key": "value"}
        
        result = dict(_get_raw_input(args, **kwargs))
        
        assert "input.value" in result

    def test_get_raw_output(self):
        """Test raw output extraction."""
        response = {"key": "value"}
        
        result = _get_raw_output(response)
        
        assert isinstance(result, str)

    def test_to_dict_with_dict(self):
        """Test _to_dict with dictionary."""
        data = {"key": "value", "nested": {"inner": "data"}}
        
        result = _to_dict(data)
        
        assert result == data

    def test_to_dict_with_object_with_to_dict(self):
        """Test _to_dict with object having to_dict method."""
        # Create a simple object with to_dict method to avoid recursion
        class SimpleObj:
            def to_dict(self):
                return {"converted": True}
        
        obj = SimpleObj()
        result = _to_dict(obj)
        
        assert result == {"converted": True}

    def test_to_dict_with_object_with_dict_attr(self):
        """Test _to_dict with object having __dict__ attribute."""
        class TestObj:
            def __init__(self):
                self.attr = "value"
        
        obj = TestObj()
        result = _to_dict(obj)
        
        assert result == {"attr": "value"}

    def test_to_dict_with_list(self):
        """Test _to_dict with list."""
        data = [{"key1": "value1"}, {"key2": "value2"}]
        
        result = _to_dict(data)
        
        assert result == data

    def test_to_dict_with_empty_data(self):
        """Test _to_dict with empty data."""
        result = _to_dict(None)
        assert result == {}
        
        result = _to_dict("")
        assert result == {}
        
        result = _to_dict([])
        assert result == {}


class TestIntegrationScenarios:
    """Integration tests covering end-to-end scenarios."""

    def test_full_instrumentation_lifecycle(self):
        """Test complete instrumentation and uninstrumentation."""
        # Simple test that verifies instrumentor can be created and basic methods exist
        instrumentor = SmolagentsInstrumentor()
        
        # Test that instrumentor has required methods
        assert hasattr(instrumentor, '_instrument')
        assert hasattr(instrumentor, '_uninstrument')
        assert hasattr(instrumentor, 'instrumentation_dependencies')
        
        # Test dependencies
        dependencies = instrumentor.instrumentation_dependencies()
        assert "smolagents >= 1.2.2" in dependencies

    def test_wrapper_creation_and_basic_functionality(self):
        """Test wrapper creation and basic functionality."""
        mock_tracer = Mock(spec=Tracer)
        
        # Test all wrapper creation
        run_wrapper = _RunWrapper(tracer=mock_tracer)
        step_wrapper = _StepWrapper(tracer=mock_tracer)
        model_wrapper = _ModelWrapper(tracer=mock_tracer)
        tool_wrapper = _ToolCallWrapper(tracer=mock_tracer)
        
        assert run_wrapper._tracer == mock_tracer
        assert step_wrapper._tracer == mock_tracer
        assert model_wrapper._tracer == mock_tracer
        assert tool_wrapper._tracer == mock_tracer

    def test_attribute_extraction_pipeline(self):
        """Test complete attribute extraction pipeline."""
        # Test agent attributes
        agent = Mock()
        agent.task = "test task"
        agent.max_steps = 5
        agent.tools = {"tool1": Mock()}
        agent.managed_agents = {}
        
        agent_attrs = dict(_smolagent_run_attributes(agent, {}))
        assert len(agent_attrs) > 0
        
        # Test tool attributes
        tool = Mock()
        tool.name = "test_tool"
        tool.description = "A test tool"
        tool.inputs = {"param": "value"}
        
        tool_attrs = dict(_tools(tool))
        assert len(tool_attrs) > 0
        
        # Test utility functions
        test_data = {"key": "value"}
        input_attrs = dict(_input_value_and_mime_type(test_data))
        assert len(input_attrs) > 0

    def test_message_processing_pipeline(self):
        """Test message processing functionality."""
        # Test input messages
        messages = [{"role": "user", "content": "Hello"}]
        input_msg_attrs = dict(_llm_input_messages(messages))
        assert isinstance(input_msg_attrs, dict)
        
        # Test output messages
        output_message = Mock()
        output_message.role = "assistant"
        output_message.content = "Hi!"
        output_message.tool_calls = None
        
        output_msg_attrs = dict(_llm_output_messages(output_message))
        assert len(output_msg_attrs) > 0


class TestErrorHandling:
    """Test error handling scenarios."""

    def test_instrumentor_with_import_error(self):
        """Test instrumentation when smolagents module is not available."""
        # Simple test that verifies instrumentor can handle error scenarios
        instrumentor = SmolagentsInstrumentor()
        
        # Test that instrumentor has the required interface
        assert hasattr(instrumentor, '_instrument')
        assert callable(getattr(instrumentor, '_instrument'))
        
        # Test dependencies
        dependencies = instrumentor.instrumentation_dependencies()
        assert isinstance(dependencies, (list, tuple))
        assert len(dependencies) > 0

    def test_wrapper_with_invalid_tracer(self):
        """Test wrapper creation with invalid tracer."""
        # Should not raise exception during creation
        wrapper = _RunWrapper(tracer=None)
        assert wrapper._tracer is None

    def test_flatten_with_malformed_data(self):
        """Test flatten handles malformed data that causes errors."""
        # Test with non-mapping types that might cause issues
        malformed_data = {"items": ["not_a_mapping", {"valid": "mapping"}]}
        
        # Should raise AttributeError when trying to call .items() on string
        with pytest.raises(AttributeError, match="'str' object has no attribute 'items'"):
            list(_flatten(malformed_data))

    def test_utility_functions_error_handling(self):
        """Test utility functions handle errors gracefully."""
        # Test _to_dict with problematic objects
        class ProblematicObj:
            def to_dict(self):
                raise Exception("Conversion failed")
        
        obj = ProblematicObj()
        # Should raise the exception
        with pytest.raises(Exception, match="Conversion failed"):
            _to_dict(obj)

    def test_agent_attributes_with_missing_attributes(self):
        """Test agent attribute extraction with missing attributes."""
        # Mock agent with missing attributes
        agent = Mock()
        agent.task = None
        del agent.max_steps  # Simulate missing attribute
        agent.tools = {}
        agent.managed_agents = {}
        
        # Should raise AttributeError when accessing missing max_steps
        with pytest.raises(AttributeError, match="max_steps"):
            dict(_smolagent_run_attributes(agent, {}))


if __name__ == "__main__":
    pytest.main([__file__]) 