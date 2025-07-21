"""
Test suite for CrewAI framework instrumentation.

Tests the instrumentation of CrewAI components:
- Task._execute_core (AGENT spans)
- Crew.kickoff (CHAIN spans) 
- ToolUsage._use (TOOL spans)
"""

import json
from enum import Enum
from typing import Any, Dict, List
from unittest.mock import MagicMock, Mock, patch

import pytest
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace.status import Status, StatusCode

from traceai_crewai import CrewAIInstrumentor
from traceai_crewai._wrappers import (
    SafeJSONEncoder,
    _ExecuteCoreWrapper,
    _KickoffWrapper,
    _ToolUseWrapper,
    _convert_to_dict,
    _flatten,
    _get_input_value,
    _prepare_args_kwargs,
)


class TestCrewAIInstrumentor:
    """Test the main CrewAI instrumentor class."""

    def test_instrumentation_dependencies(self):
        """Test that required dependencies are specified."""
        instrumentor = CrewAIInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "crewai >= 0.41.1" in deps

    @patch("traceai_crewai.import_module")
    @patch("traceai_crewai.wrap_function_wrapper")
    def test_instrument_with_defaults(self, mock_wrap, mock_import):
        """Test instrumentation with default parameters."""
        mock_tracer_provider = Mock()
        mock_task_module = Mock()
        mock_crew_module = Mock()
        mock_tool_module = Mock()
        
        mock_import.side_effect = [
            mock_task_module,
            mock_crew_module,
            mock_tool_module,
        ]
        
        instrumentor = CrewAIInstrumentor()
        instrumentor._instrument(tracer_provider=mock_tracer_provider)
        
        # Should wrap all three methods
        assert mock_wrap.call_count == 3
        
        # Verify correct module and method names
        calls = mock_wrap.call_args_list
        assert any("crewai" in str(call) and "Task._execute_core" in str(call) for call in calls)
        assert any("crewai" in str(call) and "Crew.kickoff" in str(call) for call in calls)
        assert any("crewai.tools.tool_usage" in str(call) and "ToolUsage._use" in str(call) for call in calls)

    @patch("traceai_crewai.import_module")
    def test_uninstrument(self, mock_import):
        """Test uninstrumentation restores original methods."""
        mock_task_module = Mock()
        mock_crew_module = Mock()
        mock_tool_module = Mock()
        
        mock_import.side_effect = [
            mock_task_module,
            mock_crew_module,
            mock_tool_module,
        ]
        
        instrumentor = CrewAIInstrumentor()
        
        # Set up original methods
        original_execute = Mock()
        original_kickoff = Mock()
        original_tool_use = Mock()
        
        instrumentor._original_execute_core = original_execute
        instrumentor._original_kickoff = original_kickoff
        instrumentor._original_tool_use = original_tool_use
        
        instrumentor._uninstrument()
        
        # Verify methods are restored
        assert mock_task_module.Task._execute_core == original_execute
        assert mock_crew_module.Crew.kickoff == original_kickoff
        assert mock_tool_module.ToolUsage._use == original_tool_use


class TestExecuteCoreWrapper:
    """Test the Task._execute_core wrapper for AGENT spans."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.mock_span = MagicMock(spec=trace_api.Span)
        self.mock_context_manager = MagicMock()
        self.mock_context_manager.__enter__.return_value = self.mock_span
        self.mock_context_manager.__exit__.return_value = None
        self.mock_tracer.start_as_current_span.return_value = self.mock_context_manager
        self.wrapper = _ExecuteCoreWrapper(self.mock_tracer)

    def test_suppress_instrumentation(self):
        """Test that instrumentation is suppressed when flag is set."""
        with patch.object(context_api, 'get_value', return_value=True):
            wrapped_func = Mock(return_value="result")
            
            result = self.wrapper(wrapped_func, None, (), {})
            
            assert result == "result"
            wrapped_func.assert_called_once_with()
            self.mock_tracer.start_as_current_span.assert_not_called()

    def test_task_execution_basic(self):
        """Test basic task execution wrapping."""
        # Mock objects
        mock_agent = Mock()
        mock_agent.crew = None
        mock_task = Mock()
        mock_task.key = "task_key_123"
        mock_task.id = "task_id_456"
        
        wrapped_func = Mock(__name__="_execute_core", return_value="task_result")
        
        result = self.wrapper(wrapped_func, mock_task, (mock_agent,), {})
        
        assert result == "task_result"
        
        # Verify span creation
        self.mock_tracer.start_as_current_span.assert_called_once()
        call_args = self.mock_tracer.start_as_current_span.call_args
        assert "Mock._execute_core" in call_args[0][0]
        
        # Verify attributes set
        self.mock_span.set_attribute.assert_any_call("task_key", "task_key_123")
        self.mock_span.set_attribute.assert_any_call("task_id", "task_id_456")

    def test_task_execution_with_crew(self):
        """Test task execution with crew information."""
        # Mock objects with crew
        mock_crew = Mock()
        mock_crew.key = "crew_key_789"
        mock_crew.id = "crew_id_101"
        mock_crew.share_crew = True
        
        mock_agent = Mock()
        mock_agent.crew = mock_crew
        
        mock_task = Mock()
        mock_task.key = "task_key_123"
        mock_task.id = "task_id_456"
        mock_task.description = "Test task description"
        mock_task.expected_output = "Expected output format"
        
        wrapped_func = Mock(__name__="_execute_core", return_value="task_result")
        
        result = self.wrapper(wrapped_func, mock_task, (mock_agent,), {})
        
        assert result == "task_result"
        
        # Verify crew attributes set
        self.mock_span.set_attribute.assert_any_call("crew_key", "crew_key_789")
        self.mock_span.set_attribute.assert_any_call("crew_id", "crew_id_101")
        self.mock_span.set_attribute.assert_any_call("formatted_description", "Test task description")
        self.mock_span.set_attribute.assert_any_call("formatted_expected_output", "Expected output format")

    def test_task_execution_error_handling(self):
        """Test error handling in task execution."""
        mock_agent = Mock()
        mock_agent.crew = None
        mock_task = Mock()
        mock_task.key = "task_key"
        mock_task.id = "task_id"
        
        test_exception = Exception("Task execution failed")
        wrapped_func = Mock(__name__="_execute_core", side_effect=test_exception)
        
        with pytest.raises(Exception, match="Task execution failed"):
            self.wrapper(wrapped_func, mock_task, (mock_agent,), {})
        
        # Verify error handling
        self.mock_span.set_status.assert_called_once()
        call_args = self.mock_span.set_status.call_args[0][0]
        assert call_args.status_code == StatusCode.ERROR
        assert call_args.description == "Task execution failed"
        self.mock_span.record_exception.assert_called_once_with(test_exception)

    @patch("traceai_crewai._wrappers.get_attributes_from_context")
    def test_context_attributes(self, mock_get_attrs):
        """Test that context attributes are added to spans."""
        mock_get_attrs.return_value = {"session_id": "test_session", "user_id": "test_user"}
        
        mock_agent = Mock()
        mock_agent.crew = None
        mock_task = Mock()
        mock_task.key = "task_key"
        mock_task.id = "task_id"
        
        wrapped_func = Mock(__name__="_execute_core", return_value="result")
        
        self.wrapper(wrapped_func, mock_task, (mock_agent,), {})
        
        # Verify context attributes are set
        self.mock_span.set_attributes.assert_called_once()
        call_args = self.mock_span.set_attributes.call_args[0][0]
        assert call_args["session_id"] == "test_session"
        assert call_args["user_id"] == "test_user"


class TestKickoffWrapper:
    """Test the Crew.kickoff wrapper for CHAIN spans."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.mock_span = MagicMock(spec=trace_api.Span)
        self.mock_context_manager = MagicMock()
        self.mock_context_manager.__enter__.return_value = self.mock_span
        self.mock_context_manager.__exit__.return_value = None
        self.mock_tracer.start_as_current_span.return_value = self.mock_context_manager
        self.wrapper = _KickoffWrapper(self.mock_tracer)

    def test_crew_kickoff_basic(self):
        """Test basic crew kickoff wrapping."""
        # Mock crew object
        mock_crew = Mock()
        mock_crew.__class__.__name__ = "Crew"
        mock_crew.key = "crew_key_123"
        mock_crew.id = "crew_id_456"
        mock_crew.agents = []
        mock_crew.tasks = []
        mock_crew.usage_metrics = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        
        # Mock crew output
        mock_output = Mock()
        mock_output.to_dict.return_value = {"result": "crew completed successfully"}
        
        wrapped_func = Mock(__name__="kickoff", return_value=mock_output)
        
        result = self.wrapper(wrapped_func, mock_crew, (), {"inputs": {"task": "test"}})
        
        assert result == mock_output
        
        # Verify span creation
        self.mock_tracer.start_as_current_span.assert_called_once()
        call_args = self.mock_tracer.start_as_current_span.call_args
        assert "Crew.kickoff" in call_args[0][0]
        
        # Verify basic attributes
        self.mock_span.set_attribute.assert_any_call("crew_key", "crew_key_123")
        self.mock_span.set_attribute.assert_any_call("crew_id", "crew_id_456")

    def test_crew_kickoff_with_agents_and_tasks(self):
        """Test crew kickoff with agents and tasks."""
        # Mock agent
        mock_agent = Mock()
        mock_agent.key = "agent_key_1"
        mock_agent.id = "agent_id_1"
        mock_agent.role = "researcher"
        mock_agent.goal = "research topic"
        mock_agent.backstory = "expert researcher"
        mock_agent.verbose = True
        mock_agent.max_iter = 5
        mock_agent.max_rpm = 10
        mock_agent.i18n.prompt_file = "en"
        mock_agent.allow_delegation = False
        # Create mock tool with string name
        mock_tool = Mock()
        mock_tool.name = Mock()
        mock_tool.name.casefold.return_value = "search_tool"
        mock_agent.tools = [mock_tool]
        
        # Mock task
        mock_task = Mock()
        mock_task.id = "task_id_1"
        mock_task.description = "research the topic"
        mock_task.expected_output = "comprehensive report"
        mock_task.async_execution = False
        mock_task.human_input = False
        mock_task.agent = mock_agent
        mock_task.context = None
        mock_task.tools = []
        
        # Mock crew
        mock_crew = Mock()
        mock_crew.__class__.__name__ = "Crew"
        mock_crew.key = "crew_key"
        mock_crew.id = "crew_id"
        mock_crew.agents = [mock_agent]
        mock_crew.tasks = [mock_task]
        mock_crew.usage_metrics = {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}
        
        mock_output = Mock()
        mock_output.to_dict.return_value = {"result": "success"}
        
        wrapped_func = Mock(__name__="kickoff", return_value=mock_output)
        
        result = self.wrapper(wrapped_func, mock_crew, (), {})
        
        assert result == mock_output
        
        # Verify token metrics are set
        self.mock_span.set_attribute.assert_any_call("llm.token_count.prompt", 200)
        self.mock_span.set_attribute.assert_any_call("llm.token_count.completion", 100)
        self.mock_span.set_attribute.assert_any_call("llm.token_count.total", 300)

    def test_crew_kickoff_with_new_usage_metrics(self):
        """Test crew kickoff with new usage metrics format (v0.51+)."""
        mock_crew = Mock()
        mock_crew.__class__.__name__ = "Crew"
        mock_crew.key = "crew_key"
        mock_crew.id = "crew_id"
        mock_crew.agents = []
        mock_crew.tasks = []
        
        # New format usage metrics (object with attributes)
        mock_usage = Mock()
        mock_usage.prompt_tokens = 150
        mock_usage.completion_tokens = 75
        mock_usage.total_tokens = 225
        mock_crew.usage_metrics = mock_usage
        
        mock_output = Mock()
        mock_output.to_dict.return_value = {"result": "success"}
        
        wrapped_func = Mock(__name__="kickoff", return_value=mock_output)
        
        self.wrapper(wrapped_func, mock_crew, (), {})
        
        # Verify new format token metrics
        self.mock_span.set_attribute.assert_any_call("llm.token_count.prompt", 150)
        self.mock_span.set_attribute.assert_any_call("llm.token_count.completion", 75)
        self.mock_span.set_attribute.assert_any_call("llm.token_count.total", 225)

    def test_crew_kickoff_error_handling(self):
        """Test error handling in crew kickoff."""
        mock_crew = Mock()
        mock_crew.__class__.__name__ = "Crew"
        mock_crew.key = "crew_key"
        mock_crew.id = "crew_id"
        mock_crew.agents = []  # Empty list to avoid iteration issues
        mock_crew.tasks = []   # Empty list to avoid iteration issues
        
        test_exception = Exception("Crew kickoff failed")
        wrapped_func = Mock(__name__="kickoff", side_effect=test_exception)
        
        with pytest.raises(Exception, match="Crew kickoff failed"):
            self.wrapper(wrapped_func, mock_crew, (), {})
        
        # Verify error handling
        self.mock_span.set_status.assert_called_once()
        call_args = self.mock_span.set_status.call_args[0][0]
        assert call_args.status_code == StatusCode.ERROR
        assert call_args.description == "Crew kickoff failed"
        self.mock_span.record_exception.assert_called_once_with(test_exception)

    def test_crew_output_without_to_dict(self):
        """Test crew output that doesn't have to_dict method."""
        mock_crew = Mock()
        mock_crew.__class__.__name__ = "Crew"
        mock_crew.key = "crew_key"
        mock_crew.id = "crew_id"
        mock_crew.agents = []
        mock_crew.tasks = []
        mock_crew.usage_metrics = {}
        
        # Mock output without to_dict method
        mock_output = Mock()
        mock_output.to_dict.return_value = None
        
        wrapped_func = Mock(__name__="kickoff", return_value=mock_output)
        
        result = self.wrapper(wrapped_func, mock_crew, (), {})
        
        assert result == mock_output
        
        # Should set string representation
        self.mock_span.set_attribute.assert_any_call("output.value", str(mock_output))


class TestToolUseWrapper:
    """Test the ToolUsage._use wrapper for TOOL spans."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.mock_span = MagicMock(spec=trace_api.Span)
        self.mock_context_manager = MagicMock()
        self.mock_context_manager.__enter__.return_value = self.mock_span
        self.mock_context_manager.__exit__.return_value = None
        self.mock_tracer.start_as_current_span.return_value = self.mock_context_manager
        self.wrapper = _ToolUseWrapper(self.mock_tracer)

    def test_tool_use_basic(self):
        """Test basic tool usage wrapping."""
        # Mock tool usage instance
        mock_instance = Mock()
        mock_instance.__class__.__name__ = "ToolUsage"
        mock_instance.function_calling_llm = "gpt-4"
        
        # Mock tool
        mock_tool = Mock()
        mock_tool.name = "search_tool"
        
        wrapped_func = Mock(__name__="_use", return_value={"status": "success", "result": "found data"})
        
        result = self.wrapper(wrapped_func, mock_instance, (), {"tool": mock_tool})
        
        assert result == {"status": "success", "result": "found data"}
        
        # Verify span creation
        self.mock_tracer.start_as_current_span.assert_called_once()
        call_args = self.mock_tracer.start_as_current_span.call_args
        assert "ToolUsage._use" in call_args[0][0]
        
        # Verify tool attributes
        self.mock_span.set_attribute.assert_any_call("function_calling_llm", "gpt-4")
        self.mock_span.set_attribute.assert_any_call("tool.name", "search_tool")

    def test_tool_use_without_tool(self):
        """Test tool usage without tool parameter."""
        mock_instance = Mock()
        mock_instance.__class__.__name__ = "ToolUsage"
        mock_instance.function_calling_llm = "gpt-3.5-turbo"
        
        wrapped_func = Mock(__name__="_use", return_value="no tool result")
        
        result = self.wrapper(wrapped_func, mock_instance, (), {})
        
        assert result == "no tool result"
        
        # Should set empty tool name
        self.mock_span.set_attribute.assert_any_call("tool.name", "")

    def test_tool_use_error_handling(self):
        """Test error handling in tool usage."""
        mock_instance = Mock()
        mock_instance.__class__.__name__ = "ToolUsage"
        mock_instance.function_calling_llm = "gpt-4"
        
        test_exception = Exception("Tool execution failed")
        wrapped_func = Mock(__name__="_use", side_effect=test_exception)
        
        with pytest.raises(Exception, match="Tool execution failed"):
            self.wrapper(wrapped_func, mock_instance, (), {})
        
        # Verify error handling
        self.mock_span.set_status.assert_called_once()
        call_args = self.mock_span.set_status.call_args[0][0]
        assert call_args.status_code == StatusCode.ERROR
        assert call_args.description == "Tool execution failed"
        self.mock_span.record_exception.assert_called_once_with(test_exception)

    @patch("traceai_crewai._wrappers.get_attributes_from_context")
    def test_tool_context_attributes(self, mock_get_attrs):
        """Test that context attributes are added to tool spans."""
        mock_get_attrs.return_value = {"session_id": "tool_session"}
        
        mock_instance = Mock()
        mock_instance.__class__.__name__ = "ToolUsage"
        mock_instance.function_calling_llm = "gpt-4"
        
        wrapped_func = Mock(__name__="_use", return_value="result")
        
        self.wrapper(wrapped_func, mock_instance, (), {})
        
        # Verify context attributes are set
        self.mock_span.set_attributes.assert_called_once()


class TestUtilityFunctions:
    """Test utility functions used by the wrappers."""

    def test_safe_json_encoder_basic(self):
        """Test SafeJSONEncoder with basic serializable objects."""
        encoder = SafeJSONEncoder()
        
        # Should handle normal JSON types
        result = json.dumps({"key": "value", "number": 42}, cls=SafeJSONEncoder)
        assert "key" in result
        assert "value" in result

    def test_safe_json_encoder_with_pydantic(self):
        """Test SafeJSONEncoder with pydantic-like objects."""
        # Mock object with dict method
        mock_obj = Mock()
        mock_obj.dict.return_value = {"field": "value"}
        
        encoder = SafeJSONEncoder()
        result = encoder.default(mock_obj)
        
        assert result == {"field": "value"}

    def test_safe_json_encoder_fallback(self):
        """Test SafeJSONEncoder fallback to repr."""
        class CustomClass:
            def __repr__(self):
                return "CustomClass()"
        
        encoder = SafeJSONEncoder()
        result = encoder.default(CustomClass())
        
        assert result == "CustomClass()"

    def test_flatten_simple_mapping(self):
        """Test _flatten with simple mapping."""
        data = {"key1": "value1", "key2": 42}
        result = dict(_flatten(data))
        
        assert result == {"key1": "value1", "key2": 42}

    def test_flatten_nested_mapping(self):
        """Test _flatten with nested mapping."""
        data = {
            "top": {
                "nested": "value",
                "deep": {"deeper": "deep_value"}
            }
        }
        result = dict(_flatten(data))
        
        assert result["top.nested"] == "value"
        assert result["top.deep.deeper"] == "deep_value"

    def test_flatten_with_lists(self):
        """Test _flatten with lists of mappings."""
        data = {
            "items": [
                {"name": "item1", "value": 1},
                {"name": "item2", "value": 2}
            ]
        }
        result = dict(_flatten(data))
        
        assert result["items.0.name"] == "item1"
        assert result["items.1.value"] == 2

    def test_flatten_with_enum(self):
        """Test _flatten with enum values."""
        class TestEnum(Enum):
            VALUE = "test_value"
        
        data = {"enum_field": TestEnum.VALUE}
        result = dict(_flatten(data))
        
        assert result["enum_field"] == "test_value"

    def test_flatten_with_none_values(self):
        """Test _flatten skips None values."""
        data = {"valid": "value", "null": None}
        result = dict(_flatten(data))
        
        assert "valid" in result
        assert "null" not in result

    def test_get_input_value_with_tool_calling(self):
        """Test _get_input_value with ToolCalling object."""
        # Mock ToolCalling
        mock_tool_calling = Mock()
        mock_tool_calling.tool_name = "search"
        mock_tool_calling.arguments = {"query": "test"}
        
        # Import the real class for isinstance check
        with patch("traceai_crewai._wrappers.ToolCalling", mock_tool_calling.__class__):
            mock_method = Mock()
            
            result = _get_input_value(mock_method, calling=mock_tool_calling)
            
            parsed_result = json.loads(result)
            assert parsed_result["tool_name"] == "search"
            assert parsed_result["tool_args"] == {"query": "test"}

    def test_get_input_value_with_agent(self):
        """Test _get_input_value with Agent object."""
        # Mock Agent
        mock_agent = Mock()
        mock_agent.role = "researcher"
        mock_agent.goal = "research topics"
        mock_agent.backstory = "expert in research"
        
        # Import the real class for isinstance check
        with patch("traceai_crewai._wrappers.Agent", mock_agent.__class__):
            mock_method = Mock()
            
            result = _get_input_value(mock_method, mock_agent)
            
            parsed_result = json.loads(result)
            assert parsed_result["role"] == "researcher"
            assert parsed_result["goal"] == "research topics"
            assert parsed_result["backstory"] == "expert in research"

    def test_convert_to_dict_with_to_dict_method(self):
        """Test _convert_to_dict with object having to_dict method."""
        mock_obj = Mock()
        mock_obj.to_dict.return_value = {"converted": True}
        
        result = _convert_to_dict(mock_obj)
        assert result == {"converted": True}

    def test_convert_to_dict_with_dict_attr(self):
        """Test _convert_to_dict with object having __dict__ attribute."""
        class TestObj:
            def __init__(self):
                self.field = "value"
        
        obj = TestObj()
        result = _convert_to_dict(obj)
        assert result == {"field": "value"}

    def test_convert_to_dict_with_nested_structures(self):
        """Test _convert_to_dict with nested lists and dicts."""
        mock_obj = Mock()
        mock_obj.to_dict.return_value = {"nested": True}
        
        data = {
            "list": [mock_obj, "string"],
            "dict": {"nested": mock_obj}
        }
        
        result = _convert_to_dict(data)
        assert result["list"][0] == {"nested": True}
        assert result["dict"]["nested"] == {"nested": True}

    def test_prepare_args_kwargs(self):
        """Test _prepare_args_kwargs function."""
        mock_obj = Mock()
        mock_obj.to_dict.return_value = {"converted": True}
        
        args = (mock_obj, "string")
        kwargs = {"param": mock_obj}
        
        result = _prepare_args_kwargs(args, **kwargs)
        
        assert "args" in result
        assert result["args"][0] == {"converted": True}
        assert result["param"] == {"converted": True} 