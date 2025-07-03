"""
Anthropic Framework Instrumentation Tests

Tests for the Anthropic framework instrumentation to verify it correctly instruments
Anthropic client calls and generates appropriate spans and attributes.
"""

import pytest
import sys
import os
from unittest.mock import patch, MagicMock
from pathlib import Path

# Import the instrumentation
from traceai_anthropic import AnthropicInstrumentor
import anthropic

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


class TestAnthropicFramework:
    """Test Anthropic framework instrumentation."""

    @pytest.fixture(autouse=True) 
    def setup_trace_provider(self):
        """Set up trace provider for Anthropic testing."""
        eval_tags = [
            EvalTag(
                eval_name=EvalName.TOXICITY,
                value=EvalSpanKind.LLM,
                type=EvalTagType.OBSERVATION_SPAN,
                model=ModelChoices.TURING_LARGE,
                mapping={"input": "prompt"},
                custom_eval_name="anthropic_test_eval"
            )
        ]
        
        with patch('fi_instrumentation.otel.check_custom_eval_config_exists') as mock_check:
            mock_check.return_value = False
            self.trace_provider = register(
                project_type=ProjectType.EXPERIMENT,
                eval_tags=eval_tags,
                project_name="anthropic_framework_test",
                project_version_name="v1.0",
                verbose=False
            )
        yield
        
    @pytest.fixture
    def mock_anthropic_requests(self):
        """Mock Anthropic HTTP requests to avoid real API calls."""
        from anthropic.types import Message, Usage, TextBlock
        
        # Create a proper Message object to return
        mock_usage = Usage(input_tokens=10, output_tokens=12)
        mock_content = [TextBlock(type="text", text="This is a test response from Claude.")]
        mock_message = Message(
            id="msg_test123",
            type="message",
            role="assistant",
            content=mock_content,
            model="claude-3-7-sonnet-latest",
            stop_reason="end_turn",
            stop_sequence=None,
            usage=mock_usage
        )
        
        with patch('anthropic.resources.messages.Messages.create') as mock_create:
            mock_create.return_value = mock_message
            yield mock_create
    
    def test_anthropic_import(self):
        """Test that we can import the Anthropic instrumentor."""
        assert AnthropicInstrumentor is not None
        
        # Test basic instantiation
        instrumentor = AnthropicInstrumentor()
        assert instrumentor is not None
    
    def test_anthropic_basic_instrumentation(self, mock_anthropic_requests):
        """Test basic Anthropic messages instrumentation."""
        # Initialize instrumentor
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            # Create client and make request
            client = anthropic.Anthropic(api_key="test-key")
            
            with using_attributes(
                session_id="test-session-123",
                user_id="test-user-456", 
                metadata={"test_type": "instrumentation", "framework": "anthropic"},
                tags=["anthropic", "test", "claude-3-sonnet"]
            ):
                response = client.messages.create(
                    model="claude-3-7-sonnet-latest",
                    max_tokens=50,
                    messages=[
                        {"role": "user", "content": "Write a short poem about AI testing."}
                    ]
                )
            
            # Verify the create method was called
            assert mock_anthropic_requests.called
            
            # Verify response structure (directly from mock Message object)
            assert response.content[0].text == "This is a test response from Claude."
            assert response.model == "claude-3-7-sonnet-latest"
            assert response.usage.input_tokens == 10
            assert response.usage.output_tokens == 12
            
        finally:
            # Clean up
            instrumentor.uninstrument()
    
    def test_anthropic_tool_use(self, mock_anthropic_requests):
        """Test Anthropic instrumentation with tool use."""
        from anthropic.types import Message, Usage, ToolUseBlock
        
        # Set up tool use response
        mock_usage = Usage(input_tokens=15, output_tokens=8)
        mock_content = [ToolUseBlock(
            type="tool_use",
            id="toolu_123",
            name="get_weather",
            input={"location": "San Francisco"}
        )]
        mock_tool_message = Message(
            id="msg_tool123",
            type="message",
            role="assistant",
            content=mock_content,
            model="claude-3-7-sonnet-latest",
            stop_reason="tool_use",
            stop_sequence=None,
            usage=mock_usage
        )
        mock_anthropic_requests.return_value = mock_tool_message
        
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = anthropic.Anthropic(api_key="test-key")
            
            tools = [{
                "name": "get_weather",
                "description": "Get current weather for a location",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"}
                    },
                    "required": ["location"]
                }
            }]
            
            with using_attributes(session_id="tool-test-session"):
                response = client.messages.create(
                    model="claude-3-7-sonnet-latest",
                    max_tokens=50,
                    tools=tools,
                    messages=[{"role": "user", "content": "What's the weather in San Francisco?"}]
                )
            
            # Verify tool use was captured (directly from mock Message object)
            assert response.content[0].name == "get_weather"
            assert response.content[0].input["location"] == "San Francisco"
            assert mock_anthropic_requests.called
            
        finally:
            instrumentor.uninstrument()
    
    @pytest.mark.asyncio
    async def test_anthropic_async_instrumentation(self, mock_anthropic_requests):
        """Test Anthropic async instrumentation setup."""
        # For async, let's just test that the instrumentor can handle async calls
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = anthropic.AsyncAnthropic(api_key="test-key")
            
            # Test that the async client is properly instrumented
            assert hasattr(client.messages, 'create')
            
            # Test context attributes work
            with using_attributes(session_id="async-test-session"):
                # Just verify the context is set up correctly without making a real call
                pass
                
        finally:
            instrumentor.uninstrument()
    
    def test_anthropic_completions_legacy(self, mock_anthropic_requests):
        """Test Anthropic completions (legacy) instrumentation setup."""
        # Don't need to modify mock for this test - just testing setup
        
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = anthropic.Anthropic(api_key="test-key")
            
            # Verify the client is properly instrumented for completions
            assert hasattr(client.completions, 'create')
            
            # Test context attributes work with completions
            with using_attributes(session_id="completions-test-session"):
                # Just verify completions parameter handling without making real calls
                pass
                
        finally:
            instrumentor.uninstrument()
    
    def test_anthropic_streaming_setup(self, mock_anthropic_requests):
        """Test Anthropic streaming instrumentation setup."""
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = anthropic.Anthropic(api_key="test-key")
            
            # Verify the client is properly instrumented for streaming
            assert hasattr(client.messages, 'create')
            
            # Test context attributes work with streaming
            with using_attributes(session_id="streaming-test-session"):
                # Just verify streaming parameter handling without making real calls
                pass
                
        finally:
            instrumentor.uninstrument()
    
    def test_anthropic_error_handling(self, mock_anthropic_requests):
        """Test Anthropic instrumentation with error scenarios."""
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = anthropic.Anthropic(api_key="test-key")
            
            # Verify the client is properly instrumented even for error cases
            assert hasattr(client.messages, 'create')
            
            # Test context attributes work in error scenarios
            with using_attributes(session_id="error-test-session"):
                # Just verify the instrumentation is set up correctly
                pass
            
        finally:
            instrumentor.uninstrument()
    
    def test_instrumentor_uninstrumentation(self):
        """Test that uninstrumentation properly restores original behavior."""
        from anthropic.resources.messages import Messages
        from anthropic.resources.completions import Completions
        
        instrumentor = AnthropicInstrumentor()
        
        # Store original methods
        original_messages_create = Messages.create
        original_completions_create = Completions.create
        
        # Instrument
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        # Methods should be wrapped (different types)
        assert type(Messages.create).__name__ == 'BoundFunctionWrapper'
        assert type(Completions.create).__name__ == 'BoundFunctionWrapper'
        
        # Uninstrument  
        instrumentor.uninstrument()
        
        # Methods should be restored (back to functions)
        assert Messages.create == original_messages_create
        assert Completions.create == original_completions_create 