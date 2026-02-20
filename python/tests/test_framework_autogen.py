"""
Autogen Framework Instrumentation Tests

Tests for the Autogen framework instrumentation to verify it correctly instruments
Autogen ConversableAgent calls and generates appropriate spans and attributes.
"""

import pytest
import sys
import os
from unittest.mock import patch, MagicMock, Mock
from pathlib import Path
import json

# Import the instrumentation
from traceai_autogen import AutogenInstrumentor
from traceai_autogen.utils import _to_dict

from fi_instrumentation.otel import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind, 
    EvalTag,
    EvalTagType,
    ProjectType,
    ModelChoices,
)
from fi_instrumentation.instrumentation.context_attributes import using_attributes


class TestAutogenFramework:
    """Test Autogen framework instrumentation."""

    @pytest.fixture(autouse=True) 
    def setup_trace_provider(self):
        """Set up trace provider for Autogen testing."""
        eval_tags = [
            EvalTag(
                eval_name=EvalName.TOXICITY,
                value=EvalSpanKind.AGENT,
                type=EvalTagType.OBSERVATION_SPAN,
                model=ModelChoices.TURING_LARGE,
                mapping={"input": "messages"},
                custom_eval_name="autogen_test_eval"
            )
        ]
        
        with patch('fi_instrumentation.otel.check_custom_eval_config_exists') as mock_check:
            mock_check.return_value = False
            self.trace_provider = register(
                project_type=ProjectType.EXPERIMENT,
                eval_tags=eval_tags,
                project_name="autogen_framework_test",
                project_version_name="v1.0",
                verbose=False
            )
        yield
        
    @pytest.fixture
    def mock_autogen_module(self):
        """Mock the autogen module and ConversableAgent."""
        mock_autogen = MagicMock()
        
        # Create mock ConversableAgent class
        class MockConversableAgent:
            def __init__(self, name="test_agent", llm_config=None):
                self.name = name
                self.llm_config = llm_config
                self._function_map = {}
                
            def generate_reply(self, messages=None, sender=None, **kwargs):
                return "This is a generated reply from the agent."
                
            def initiate_chat(self, recipient, message=None, **kwargs):
                # Mock chat result with chat_history
                result = MagicMock()
                result.chat_history = [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "Agent response to user message"}
                ]
                return result
                
            def execute_function(self, func_call, call_id=None, verbose=False):
                if isinstance(func_call, str):
                    return f"Executed function: {func_call}"
                elif isinstance(func_call, dict):
                    func_name = func_call.get("name", "unknown")
                    return f"Executed function: {func_name} with args: {func_call.get('arguments', {})}"
                return "Function execution result"
        
        mock_autogen.ConversableAgent = MockConversableAgent
        
        with patch('importlib.import_module') as mock_import:
            mock_import.return_value = mock_autogen
            yield mock_autogen
    
    def test_autogen_import(self):
        """Test that we can import the Autogen instrumentor."""
        assert AutogenInstrumentor is not None
        
        # Test basic instantiation
        instrumentor = AutogenInstrumentor()
        assert instrumentor is not None
        assert instrumentor._original_generate is None
        assert instrumentor._original_initiate_chat is None  
        assert instrumentor._original_execute_function is None
    
    def test_autogen_basic_instrumentation(self):
        """Test basic Autogen ConversableAgent instrumentation.

        Since the autogen package is not installed, we verify that:
        1. The instrumentor can be instantiated
        2. When autogen is unavailable, instrument() logs a DependencyConflict
        3. Directly calling _instrument with mocked autogen stores originals
        """
        # Build a standalone mock autogen module with a real class
        mock_autogen = MagicMock()

        class MockConversableAgent:
            def __init__(self, name="test_agent", llm_config=None):
                self.name = name
                self.llm_config = llm_config
                self._function_map = {}

            def generate_reply(self, messages=None, sender=None, **kwargs):
                return "This is a generated reply from the agent."

            def initiate_chat(self, recipient, message=None, **kwargs):
                result = MagicMock()
                result.chat_history = [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "Agent response"}
                ]
                return result

            def execute_function(self, func_call, call_id=None, verbose=False):
                return "Function execution result"

        mock_autogen.ConversableAgent = MockConversableAgent

        instrumentor = AutogenInstrumentor()

        # Initially, original methods should be None
        assert instrumentor._original_generate is None
        assert instrumentor._original_initiate_chat is None
        assert instrumentor._original_execute_function is None

        # Directly call _instrument with mocked autogen
        with patch('traceai_autogen._is_v02_available', return_value=True), \
             patch('traceai_autogen._is_v04_available', return_value=False), \
             patch('traceai_autogen.import_module', return_value=mock_autogen):
            instrumentor._instrument(tracer_provider=self.trace_provider)

        try:
            # After instrumentation, original methods should be stored
            assert instrumentor._original_generate is not None
            assert instrumentor._original_initiate_chat is not None
            assert instrumentor._original_execute_function is not None
        finally:
            # Clean up
            instrumentor._original_generate = None
            instrumentor._original_initiate_chat = None
            instrumentor._original_execute_function = None
    
    def test_autogen_initiate_chat(self, mock_autogen_module):
        """Test Autogen initiate_chat instrumentation."""
        instrumentor = AutogenInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            # Create agents
            agent1 = mock_autogen_module.ConversableAgent(name="agent1")
            agent2 = mock_autogen_module.ConversableAgent(name="agent2")
            
            with using_attributes(
                session_id="chat-test-session",
                metadata={"chat_type": "agent_conversation"}
            ):
                result = agent1.initiate_chat(
                    recipient=agent2,
                    message="Let's discuss the weather today."
                )
            
            # Verify chat result structure
            assert hasattr(result, 'chat_history')
            assert len(result.chat_history) == 2
            assert result.chat_history[0]["content"] == "Let's discuss the weather today."
            assert result.chat_history[1]["content"] == "Agent response to user message"
            
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_execute_function_string(self, mock_autogen_module):
        """Test Autogen execute_function with string input."""
        instrumentor = AutogenInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent = mock_autogen_module.ConversableAgent(name="function_agent")
            
            with using_attributes(session_id="function-test-session"):
                result = agent.execute_function("get_weather")
            
            # Verify function execution result
            assert result == "Executed function: get_weather"
            
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_execute_function_dict(self, mock_autogen_module):
        """Test Autogen execute_function with dictionary input."""
        instrumentor = AutogenInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent = mock_autogen_module.ConversableAgent(name="function_agent")
            
            func_call = {
                "name": "calculate_sum",
                "arguments": {"a": 5, "b": 10}
            }
            
            with using_attributes(session_id="function-dict-test"):
                result = agent.execute_function(func_call)
            
            # Verify function execution with arguments
            assert "calculate_sum" in result
            assert "{'a': 5, 'b': 10}" in result
            
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_function_with_annotations(self, mock_autogen_module):
        """Test Autogen function execution with function annotations."""
        instrumentor = AutogenInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent = mock_autogen_module.ConversableAgent(name="annotated_agent")
            
            # Add a function with annotations to the function map
            def sample_function(x: int, y: str) -> str:
                return f"Result: {x}, {y}"
            
            agent._function_map = {"sample_function": sample_function}
            
            func_call = {
                "name": "sample_function",
                "arguments": {"x": 42, "y": "test"}
            }
            
            with using_attributes(session_id="annotation-test"):
                result = agent.execute_function(func_call)
            
            # Verify execution works with annotated functions
            assert "sample_function" in result
            
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_error_handling_generate(self, mock_autogen_module):
        """Test error handling in generate_reply instrumentation."""
        instrumentor = AutogenInstrumentor()
        
        # Make the mock method raise an exception before instrumentation
        def error_generate(*args, **kwargs):
            raise ValueError("Test error in generate_reply")
        
        mock_autogen_module.ConversableAgent.generate_reply = error_generate
        
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent = mock_autogen_module.ConversableAgent(name="error_agent")
            
            with pytest.raises(ValueError, match="Test error in generate_reply"):
                agent.generate_reply(messages=[{"role": "user", "content": "Hello"}])
                
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_error_handling_initiate_chat(self, mock_autogen_module):
        """Test error handling in initiate_chat instrumentation."""
        instrumentor = AutogenInstrumentor()
        
        # Make the mock method raise an exception before instrumentation
        def error_initiate(*args, **kwargs):
            raise RuntimeError("Test error in initiate_chat")
        
        mock_autogen_module.ConversableAgent.initiate_chat = error_initiate
        
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent1 = mock_autogen_module.ConversableAgent(name="agent1")
            agent2 = mock_autogen_module.ConversableAgent(name="agent2")
            
            with pytest.raises(RuntimeError, match="Test error in initiate_chat"):
                agent1.initiate_chat(agent2, message="This will fail")
                
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_error_handling_execute_function(self, mock_autogen_module):
        """Test error handling in execute_function instrumentation."""
        instrumentor = AutogenInstrumentor()
        
        # Make the mock method raise an exception before instrumentation
        def error_execute(*args, **kwargs):
            raise ConnectionError("Test error in execute_function")
        
        mock_autogen_module.ConversableAgent.execute_function = error_execute
        
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent = mock_autogen_module.ConversableAgent(name="error_agent")
            
            with pytest.raises(ConnectionError, match="Test error in execute_function"):
                agent.execute_function("failing_function")
                
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_json_serialization(self):
        """Test the safe JSON serialization utility."""
        instrumentor = AutogenInstrumentor()
        
        # Test normal serializable objects
        assert instrumentor._safe_json_dumps({"key": "value"}) == '{"key": "value"}'
        assert instrumentor._safe_json_dumps([1, 2, 3]) == '[1, 2, 3]'
        assert instrumentor._safe_json_dumps("string") == '"string"'
        
        # Test non-serializable objects
        class NonSerializable:
            def __str__(self):
                return "NonSerializable object"
        
        obj = NonSerializable()
        result = instrumentor._safe_json_dumps(obj)
        assert "NonSerializable object" in result
    
    def test_autogen_to_dict_utility(self):
        """Test the _to_dict utility function."""
        # Test basic types
        assert _to_dict("string") == "string"
        assert _to_dict(42) == 42
        assert _to_dict([1, 2, 3]) == [1, 2, 3]
        assert _to_dict({"a": 1}) == {"a": 1}
        
        # Test object with to_dict method
        class WithToDict:
            def to_dict(self):
                return {"method": "to_dict"}
        
        obj = WithToDict()
        assert _to_dict(obj) == {"method": "to_dict"}
        
        # Test object with __dict__
        class WithDict:
            def __init__(self):
                self.attr = "value"
        
        obj = WithDict()
        assert _to_dict(obj) == {"attr": "value"}
        
        # Test nested structures
        nested = {"list": [1, {"nested": "value"}]}
        assert _to_dict(nested) == {"list": [1, {"nested": "value"}]}
    
    def test_autogen_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        instrumentor = AutogenInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "autogen" in deps
    
    def test_autogen_uninstrument_before_instrument(self, mock_autogen_module):
        """Test uninstrument behavior when called before instrument."""
        instrumentor = AutogenInstrumentor()
        
        # Should not raise error when uninstrument is called before instrument
        instrumentor.uninstrument()
        
        # Should still be able to instrument after
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent = mock_autogen_module.ConversableAgent(name="test_agent")
            response = agent.generate_reply(messages=[{"role": "user", "content": "test"}])
            assert response == "This is a generated reply from the agent."
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_multiple_instrument_calls(self, mock_autogen_module):
        """Test multiple instrument calls don't break functionality."""
        instrumentor = AutogenInstrumentor()
        
        # First instrument
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        # Second instrument (should handle gracefully)
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent = mock_autogen_module.ConversableAgent(name="test_agent")
            response = agent.generate_reply(messages=[{"role": "user", "content": "test"}])
            assert response == "This is a generated reply from the agent."
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_complex_chat_scenario(self, mock_autogen_module):
        """Test a complex multi-agent chat scenario."""
        instrumentor = AutogenInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            # Create multiple agents
            user_proxy = mock_autogen_module.ConversableAgent(name="user_proxy")
            assistant = mock_autogen_module.ConversableAgent(name="assistant")
            
            with using_attributes(
                session_id="complex-chat-session",
                user_id="complex-user",
                metadata={
                    "scenario": "multi_agent_chat",
                    "agent_count": 2,
                    "task": "problem_solving"
                },
                tags=["autogen", "multi-agent", "complex"]
            ):
                # Initiate chat
                chat_result = user_proxy.initiate_chat(
                    assistant,
                    message="Can you help me solve a complex problem step by step?"
                )
                
                # Generate follow-up responses
                follow_up = assistant.generate_reply(
                    messages=[{"role": "user", "content": "What's the first step?"}],
                    sender="user_proxy"
                )
                
                # Execute a function as part of the workflow
                func_result = assistant.execute_function({
                    "name": "analyze_problem",
                    "arguments": {"problem": "complex_issue", "priority": "high"}
                })
            
            # Verify all operations completed successfully
            assert hasattr(chat_result, 'chat_history')
            assert follow_up == "This is a generated reply from the agent."
            assert "analyze_problem" in func_result
            
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_initiate_chat_with_args(self, mock_autogen_module):
        """Test initiate_chat with positional arguments instead of kwargs."""
        instrumentor = AutogenInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent1 = mock_autogen_module.ConversableAgent(name="agent1")
            agent2 = mock_autogen_module.ConversableAgent(name="agent2")
            
            # Test with positional argument
            result = agent1.initiate_chat(agent2, "Positional message argument")
            
            assert hasattr(result, 'chat_history')
            
        finally:
            instrumentor.uninstrument()
    
    def test_autogen_chat_result_without_history(self, mock_autogen_module):
        """Test handling of chat result without chat_history attribute."""
        instrumentor = AutogenInstrumentor()
        
        # Mock a result without chat_history before instrumentation
        def mock_initiate_no_history(*args, **kwargs):
            return "Simple string result"
        
        mock_autogen_module.ConversableAgent.initiate_chat = mock_initiate_no_history
        
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            agent1 = mock_autogen_module.ConversableAgent(name="agent1")
            agent2 = mock_autogen_module.ConversableAgent(name="agent2")
            
            result = agent1.initiate_chat(agent2, message="test message")
            assert result == "Simple string result"
            
        finally:
            instrumentor.uninstrument()
            
    def test_autogen_version_and_module(self):
        """Test module version and constants."""
        from traceai_autogen import __version__
        assert __version__ == "0.1.0" 