"""
OpenAI Framework Instrumentation Tests

Tests for the OpenAI framework instrumentation to verify it correctly instruments
OpenAI client calls and generates appropriate spans and attributes.
"""

import pytest
import sys
import os
from unittest.mock import patch, MagicMock
from pathlib import Path

# Import the instrumentation
from traceai_openai import OpenAIInstrumentor
import openai

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


class TestOpenAIFramework:
    """Test OpenAI framework instrumentation."""

    @pytest.fixture(autouse=True) 
    def setup_trace_provider(self):
        """Set up trace provider for OpenAI testing."""
        eval_tags = [
            EvalTag(
                eval_name=EvalName.TOXICITY,
                value=EvalSpanKind.LLM,
                type=EvalTagType.OBSERVATION_SPAN,
                model=ModelChoices.TURING_LARGE,
                mapping={"input": "prompt"},
                custom_eval_name="openai_test_eval"
            )
        ]
        
        with patch('fi_instrumentation.otel.check_custom_eval_config_exists') as mock_check:
            mock_check.return_value = False
            self.trace_provider = register(
                project_type=ProjectType.EXPERIMENT,
                eval_tags=eval_tags,
                project_name="openai_framework_test",
                project_version_name="v1.0",
                verbose=False
            )
        yield
        
    @pytest.fixture
    def mock_openai_requests(self):
        """Mock OpenAI HTTP requests to avoid real API calls."""
        with patch('httpx.Client.send') as mock_request:
            # Set up mock response for chat completions
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.text = '''{"id": "chatcmpl-test123", "object": "chat.completion", "created": 1699999999, "model": "gpt-4o", "choices": [{"index": 0, "message": {"role": "assistant", "content": "This is a test response from GPT."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 15, "completion_tokens": 10, "total_tokens": 25}}'''
            mock_response.json.return_value = {
                "id": "chatcmpl-test123",
                "object": "chat.completion",
                "created": 1699999999,
                "model": "gpt-4o",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "This is a test response from GPT."
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 10,
                    "total_tokens": 25
                }
            }
            mock_request.return_value = mock_response
            yield mock_request
    
    def test_openai_import(self):
        """Test that we can import the OpenAI instrumentor."""
        assert OpenAIInstrumentor is not None
        
        # Test basic instantiation
        instrumentor = OpenAIInstrumentor()
        assert instrumentor is not None
    
    def test_openai_basic_instrumentation(self, mock_openai_requests):
        """Test basic OpenAI chat completion instrumentation."""
        # Initialize instrumentor
        instrumentor = OpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            # Create client and make request
            client = openai.OpenAI(api_key="test-key")
            
            with using_attributes(
                session_id="test-session-123",
                user_id="test-user-456", 
                metadata={"test_type": "instrumentation", "framework": "openai"},
                tags=["openai", "test", "gpt-4o"]
            ):
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "Write a short poem about software testing."}
                    ],
                    max_tokens=50,
                    temperature=0.7
                )
            
            # Verify the HTTP request was made
            assert mock_openai_requests.called
            
            # Parse the response to get the actual ChatCompletion object
            parsed_response = response.parse()
            
            # Verify response structure (parsed from mock JSON)
            assert parsed_response.choices[0].message.content == "This is a test response from GPT."
            assert parsed_response.model == "gpt-4o"
            assert parsed_response.usage.total_tokens == 25
            
        finally:
            # Clean up
            instrumentor.uninstrument()
    
    def test_openai_function_calling(self, mock_openai_requests):
        """Test OpenAI instrumentation with function calling."""
        # Set up function calling response
        mock_openai_requests.return_value.text = '''{"id": "chatcmpl-function123", "object": "chat.completion", "created": 1699999999, "model": "gpt-4o", "choices": [{"index": 0, "message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_123", "type": "function", "function": {"name": "get_weather", "arguments": "{\\"location\\": \\"San Francisco\\"}"}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}}'''
        mock_openai_requests.return_value.json.return_value = {
            "id": "chatcmpl-function123",
            "object": "chat.completion",
            "created": 1699999999,
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "San Francisco"}'
                        }
                    }]
                },
                "finish_reason": "tool_calls"
            }],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 15,
                "total_tokens": 35
            }
        }
        
        instrumentor = OpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = openai.OpenAI(api_key="test-key")
            
            tools = [{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string", "description": "City name"}
                        },
                        "required": ["location"]
                    }
                }
            }]
            
            with using_attributes(session_id="function-test-session"):
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "What's the weather in San Francisco?"}],
                    tools=tools,
                    max_tokens=50
                )
            
            # Parse the response to get the actual ChatCompletion object
            parsed_response = response.parse()
            
            # Verify function calling was captured
            assert parsed_response.choices[0].message.tool_calls[0].function.name == "get_weather"
            assert mock_openai_requests.called
            
        finally:
            instrumentor.uninstrument()
    
    @pytest.mark.asyncio
    async def test_openai_async_instrumentation(self, mock_openai_requests):
        """Test OpenAI async instrumentation."""
        # For async, let's just test that the instrumentor can handle async calls
        instrumentor = OpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = openai.AsyncOpenAI(api_key="test-key")
            
            # Test that the async client is properly instrumented
            assert hasattr(client, 'request')
            assert type(client.request).__name__ == 'BoundFunctionWrapper'
            
            # Test context attributes work
            with using_attributes(session_id="async-test-session"):
                # Just verify the context is set up correctly without making a real call
                pass
                
        finally:
            instrumentor.uninstrument()
    
    def test_openai_streaming(self, mock_openai_requests):
        """Test OpenAI streaming instrumentation setup."""
        instrumentor = OpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = openai.OpenAI(api_key="test-key")
            
            # Verify the client is properly instrumented for streaming
            assert hasattr(client, 'request')
            assert type(client.request).__name__ == 'BoundFunctionWrapper'
            
            # Test context attributes work with streaming
            with using_attributes(session_id="streaming-test-session"):
                # Just verify streaming parameter handling without making real calls
                # The complex streaming test would require real API setup
                pass
                
        finally:
            instrumentor.uninstrument()
    
    def test_openai_error_handling(self, mock_openai_requests):
        """Test OpenAI instrumentation with error scenarios."""
        instrumentor = OpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = openai.OpenAI(api_key="test-key")
            
            # Verify the client is properly instrumented even for error cases
            assert hasattr(client, 'request')
            assert type(client.request).__name__ == 'BoundFunctionWrapper'
            
            # Test context attributes work in error scenarios
            with using_attributes(session_id="error-test-session"):
                # Just verify the instrumentation is set up correctly
                # Real error testing would require more complex mock setup
                pass
            
        finally:
            instrumentor.uninstrument()
    
    def test_instrumentor_uninstrumentation(self):
        """Test that uninstrumentation properly restores original behavior."""
        instrumentor = OpenAIInstrumentor()
        
        # Store original methods
        original_request = openai.OpenAI.request
        original_async_request = openai.AsyncOpenAI.request
        
        # Instrument
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        # Methods should be wrapped (different types)
        assert type(openai.OpenAI.request).__name__ == 'BoundFunctionWrapper'
        assert type(openai.AsyncOpenAI.request).__name__ == 'BoundFunctionWrapper'
        
        # Uninstrument  
        instrumentor.uninstrument()
        
        # Methods should be restored (back to functions)
        assert openai.OpenAI.request == original_request
        assert openai.AsyncOpenAI.request == original_async_request 

class TestProcessInputData:
    """Unit tests for _process_input_data (issue #151 regression)."""

    @pytest.fixture
    def span(self):
        from traceai_openai._span_io_handler import _process_input_data

        mock = MagicMock()
        yield mock, _process_input_data

    def test_multi_message_input_preserves_all_messages(self, span):
        """INPUT_VALUE must keep every message's text, not just the first."""
        from fi_instrumentation.fi_types import SpanAttributes

        mock, process = span
        process(
            [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "What is 2+2?"},
                {"role": "assistant", "content": "4"},
                {"role": "user", "content": "And 3+3?"},
            ],
            mock,
        )
        values = [
            c.args[1]
            for c in mock.set_attribute.call_args_list
            if c.args[0] == SpanAttributes.INPUT_VALUE
        ]
        final = values[-1]
        for text in ("You are helpful.", "What is 2+2?", "4", "And 3+3?"):
            assert text in final

    def test_multimodal_input_keeps_text_and_image_placeholders(self, span):
        """Image parts become input_images placeholders inside the joined text."""
        from fi_instrumentation.fi_types import SpanAttributes

        mock, process = span
        process(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/a.png"},
                        },
                        {"type": "text", "text": "in detail"},
                    ],
                }
            ],
            mock,
        )
        attrs = {c.args[0]: c.args[1] for c in mock.set_attribute.call_args_list}
        final = attrs[SpanAttributes.INPUT_VALUE]
        assert "describe this" in final
        assert "in detail" in final
        assert f"{{{SpanAttributes.INPUT_IMAGES}.0}}" in final
        assert "https://example.com/a.png" in attrs[SpanAttributes.INPUT_IMAGES]

    def test_single_message_still_sets_value(self, span):
        from fi_instrumentation.fi_types import SpanAttributes

        mock, process = span
        process([{"role": "user", "content": "hello"}], mock)
        attrs = {c.args[0]: c.args[1] for c in mock.set_attribute.call_args_list}
        assert "hello" in attrs[SpanAttributes.INPUT_VALUE]

    def test_non_list_input_serializes(self, span):
        from fi_instrumentation.fi_types import SpanAttributes

        mock, process = span
        process("plain prompt", mock)
        attrs = {c.args[0]: c.args[1] for c in mock.set_attribute.call_args_list}
        assert attrs[SpanAttributes.INPUT_VALUE] == '"plain prompt"'
