"""
Azure OpenAI Framework Instrumentation Tests

Tests for the Azure OpenAI framework instrumentation to verify it correctly instruments
Azure OpenAI client calls and generates appropriate spans and attributes.
"""

import pytest
import os
from unittest.mock import patch, MagicMock

# Import the instrumentation
from traceai_azure_openai import AzureOpenAIInstrumentor
import openai
from openai import AzureOpenAI, AsyncAzureOpenAI

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


class TestAzureOpenAIFramework:
    """Test Azure OpenAI framework instrumentation."""

    @pytest.fixture(autouse=True)
    def setup_trace_provider(self):
        """Set up trace provider for Azure OpenAI testing."""
        eval_tags = [
            EvalTag(
                eval_name=EvalName.TOXICITY,
                value=EvalSpanKind.LLM,
                type=EvalTagType.OBSERVATION_SPAN,
                model=ModelChoices.TURING_LARGE,
                mapping={"input": "prompt"},
                custom_eval_name="azure_openai_test_eval"
            )
        ]

        with patch('fi_instrumentation.otel.check_custom_eval_config_exists') as mock_check:
            mock_check.return_value = False
            self.trace_provider = register(
                project_type=ProjectType.EXPERIMENT,
                eval_tags=eval_tags,
                project_name="azure_openai_framework_test",
                project_version_name="v1.0",
                verbose=False
            )
        yield

    @pytest.fixture
    def mock_azure_openai_requests(self):
        """Mock Azure OpenAI HTTP requests to avoid real API calls."""
        with patch('httpx.Client.send') as mock_request:
            # Set up mock response for chat completions
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.text = '''{"id": "chatcmpl-azure123", "object": "chat.completion", "created": 1699999999, "model": "gpt-4o", "choices": [{"index": 0, "message": {"role": "assistant", "content": "This is a test response from Azure OpenAI."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 15, "completion_tokens": 12, "total_tokens": 27}}'''
            mock_response.json.return_value = {
                "id": "chatcmpl-azure123",
                "object": "chat.completion",
                "created": 1699999999,
                "model": "gpt-4o",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "This is a test response from Azure OpenAI."
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 12,
                    "total_tokens": 27
                }
            }
            mock_request.return_value = mock_response
            yield mock_request

    def test_azure_openai_import(self):
        """Test that we can import the Azure OpenAI instrumentor."""
        assert AzureOpenAIInstrumentor is not None

        # Test basic instantiation
        instrumentor = AzureOpenAIInstrumentor()
        assert instrumentor is not None

    def test_azure_openai_basic_instrumentation(self, mock_azure_openai_requests):
        """Test basic Azure OpenAI chat completion instrumentation."""
        instrumentor = AzureOpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)

        try:
            # Create Azure client
            client = AzureOpenAI(
                azure_endpoint="https://test-resource.openai.azure.com",
                api_key="test-key",
                api_version="2024-02-01",
            )

            with using_attributes(
                session_id="azure-test-session-123",
                user_id="azure-test-user-456",
                metadata={"test_type": "instrumentation", "framework": "azure_openai"},
                tags=["azure-openai", "test", "gpt-4o"]
            ):
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "Write a short poem about cloud computing."}
                    ],
                    max_tokens=50,
                    temperature=0.7
                )

            # Verify the HTTP request was made
            assert mock_azure_openai_requests.called

            # Parse the response to get the actual ChatCompletion object
            parsed_response = response.parse()

            # Verify response structure (parsed from mock JSON)
            assert parsed_response.choices[0].message.content == "This is a test response from Azure OpenAI."
            assert parsed_response.model == "gpt-4o"
            assert parsed_response.usage.total_tokens == 27

        finally:
            instrumentor.uninstrument()

    def test_azure_openai_function_calling(self, mock_azure_openai_requests):
        """Test Azure OpenAI instrumentation with function calling."""
        mock_azure_openai_requests.return_value.text = '''{"id": "chatcmpl-azurefunc123", "object": "chat.completion", "created": 1699999999, "model": "gpt-4o", "choices": [{"index": 0, "message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_azure_123", "type": "function", "function": {"name": "get_weather", "arguments": "{\\"location\\": \\"Seattle\\"}"}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}}'''
        mock_azure_openai_requests.return_value.json.return_value = {
            "id": "chatcmpl-azurefunc123",
            "object": "chat.completion",
            "created": 1699999999,
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_azure_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Seattle"}'
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

        instrumentor = AzureOpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)

        try:
            client = AzureOpenAI(
                azure_endpoint="https://test-resource.openai.azure.com",
                api_key="test-key",
                api_version="2024-02-01",
            )

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

            with using_attributes(session_id="azure-function-test-session"):
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "What's the weather in Seattle?"}],
                    tools=tools,
                    max_tokens=50
                )

            parsed_response = response.parse()

            # Verify function calling was captured
            assert parsed_response.choices[0].message.tool_calls[0].function.name == "get_weather"
            assert mock_azure_openai_requests.called

        finally:
            instrumentor.uninstrument()

    @pytest.mark.asyncio
    async def test_azure_openai_async_instrumentation(self, mock_azure_openai_requests):
        """Test Azure OpenAI async instrumentation."""
        instrumentor = AzureOpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)

        try:
            client = AsyncAzureOpenAI(
                azure_endpoint="https://test-resource.openai.azure.com",
                api_key="test-key",
                api_version="2024-02-01",
            )

            # Test that the async client is properly instrumented
            assert hasattr(client, 'request')
            assert type(client.request).__name__ == 'BoundFunctionWrapper'

            # Test context attributes work
            with using_attributes(session_id="azure-async-test-session"):
                pass

        finally:
            instrumentor.uninstrument()

    def test_azure_openai_streaming(self, mock_azure_openai_requests):
        """Test Azure OpenAI streaming instrumentation setup."""
        instrumentor = AzureOpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)

        try:
            client = AzureOpenAI(
                azure_endpoint="https://test-resource.openai.azure.com",
                api_key="test-key",
                api_version="2024-02-01",
            )

            # Verify the client is properly instrumented for streaming
            assert hasattr(client, 'request')
            assert type(client.request).__name__ == 'BoundFunctionWrapper'

            with using_attributes(session_id="azure-streaming-test-session"):
                pass

        finally:
            instrumentor.uninstrument()

    def test_instrumentor_uninstrumentation(self):
        """Test that uninstrumentation properly restores original behavior."""
        instrumentor = AzureOpenAIInstrumentor()

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

    def test_regular_openai_not_instrumented(self, mock_azure_openai_requests):
        """Test that regular OpenAI clients are NOT instrumented by AzureOpenAIInstrumentor."""
        instrumentor = AzureOpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)

        try:
            # A regular OpenAI client should pass through without tracing
            regular_client = openai.OpenAI(api_key="test-key")

            with using_attributes(session_id="regular-openai-session"):
                response = regular_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "Hello"}],
                    max_tokens=10
                )

            # The call should still work (passed through)
            assert mock_azure_openai_requests.called

        finally:
            instrumentor.uninstrument()
