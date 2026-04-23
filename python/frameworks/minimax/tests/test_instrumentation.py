"""Tests for MiniMax instrumentation."""
import json
from unittest.mock import MagicMock, patch

import pytest

from traceai_minimax import MiniMaxInstrumentor


class TestMiniMaxInstrumentor:
    """Tests for MiniMaxInstrumentor."""

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        instrumentor = MiniMaxInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "openai >= 1.0.0" in deps


class TestChatCompletionRequestAttributesExtractor:
    """Tests for chat completion request attributes extraction."""

    def test_extract_chat_attributes(self):
        """Test extracting chat request attributes."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "MiniMax-M2.7",
            "messages": [
                {"role": "user", "content": "Hello!"}
            ],
            "temperature": 0.7,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "LLM"
        assert attributes["llm.provider"] == "minimax"
        assert attributes["llm.model"] == "MiniMax-M2.7"

    def test_default_model_is_m27(self):
        """Test that default model is MiniMax-M2.7 when not specified."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "messages": [{"role": "user", "content": "Hello!"}],
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["llm.model"] == "MiniMax-M2.7"

    def test_extract_m27_highspeed_model(self):
        """Test extracting attributes for MiniMax-M2.7-highspeed model."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "MiniMax-M2.7-highspeed",
            "messages": [{"role": "user", "content": "Hello"}],
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["llm.model"] == "MiniMax-M2.7-highspeed"

    def test_extract_chat_with_system_message(self):
        """Test extracting chat with system message."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "MiniMax-M2.5",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        # Should have messages
        assert any("input_messages" in k for k in extra_attrs.keys())

    def test_extract_tools(self):
        """Test extracting tools from request."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "MiniMax-M2.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather info",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert extra_attrs.get("minimax.tools_count") == 1

    def test_extract_invocation_parameters(self):
        """Test extracting invocation parameters."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "MiniMax-M2.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "max_tokens": 1024,
            "top_p": 0.9,
            "frequency_penalty": 0.5,
            "presence_penalty": 0.5,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert "llm.invocation_parameters" in attributes
        params = json.loads(attributes["llm.invocation_parameters"])
        assert params["temperature"] == 0.7
        assert params["max_tokens"] == 1024

    def test_extract_highspeed_model(self):
        """Test extracting attributes for MiniMax-M2.5-highspeed model."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "MiniMax-M2.5-highspeed",
            "messages": [{"role": "user", "content": "Hello"}],
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["llm.model"] == "MiniMax-M2.5-highspeed"


class TestChatCompletionResponseAttributesExtractor:
    """Tests for chat completion response attributes extraction."""

    def test_extract_token_counts(self):
        """Test extracting token counts from response."""
        from traceai_minimax._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "id": "test-id",
            "choices": [
                {"message": {"role": "assistant", "content": "Hello!"}}
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 15,
                "total_tokens": 25,
            },
        }

        attributes = dict(extractor.get_attributes(response))

        assert attributes.get("llm.token_count.prompt") == 10
        assert attributes.get("llm.token_count.completion") == 15

    def test_extract_response_content(self):
        """Test extracting response content."""
        from traceai_minimax._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "choices": [
                {"message": {"role": "assistant", "content": "Paris is the capital."}}
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        # Content is captured in output messages
        assert extra_attrs.get("llm.output_messages.0.message.content") == "Paris is the capital."

    def test_extract_tool_calls(self):
        """Test extracting tool calls from response."""
        from traceai_minimax._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"function": {"name": "get_weather", "arguments": '{"location": "Paris"}'}}
                        ],
                    }
                }
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert extra_attrs.get("minimax.tool_calls_count") == 1

    def test_extract_response_id(self):
        """Test extracting response ID."""
        from traceai_minimax._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "id": "chatcmpl-abc123",
            "choices": [
                {"message": {"role": "assistant", "content": "Hello!"}}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }

        attributes = dict(extractor.get_attributes(response))

        assert attributes.get("minimax.response_id") == "chatcmpl-abc123"


class TestUtils:
    """Tests for utility functions."""

    def test_to_dict_with_dict(self):
        """Test _to_dict with dictionary input."""
        from traceai_minimax._utils import _to_dict

        input_dict = {"key": "value"}
        result = _to_dict(input_dict)

        assert result == input_dict

    def test_to_dict_with_none(self):
        """Test _to_dict with None input."""
        from traceai_minimax._utils import _to_dict

        result = _to_dict(None)

        assert result == {}

    def test_to_dict_with_object(self):
        """Test _to_dict with object having model_dump."""
        from traceai_minimax._utils import _to_dict

        class MockResponse:
            def model_dump(self):
                return {"key": "value"}

        result = _to_dict(MockResponse())

        assert result == {"key": "value"}

    def test_is_minimax_client_with_minimax_url(self):
        """Test is_minimax_client with MiniMax URL."""
        from traceai_minimax._utils import is_minimax_client

        mock_client = MagicMock()
        mock_client.base_url = "https://api.minimax.io/v1"

        assert is_minimax_client(mock_client) is True

    def test_is_minimax_client_with_openai_url(self):
        """Test is_minimax_client with OpenAI URL."""
        from traceai_minimax._utils import is_minimax_client

        mock_client = MagicMock()
        mock_client.base_url = "https://api.openai.com/v1"

        assert is_minimax_client(mock_client) is False

    def test_is_minimax_client_with_minimax_chat_url(self):
        """Test is_minimax_client with api.minimax.chat URL."""
        from traceai_minimax._utils import is_minimax_client

        mock_client = MagicMock()
        mock_client.base_url = "https://api.minimax.chat/v1"

        assert is_minimax_client(mock_client) is True


class TestRealWorldScenarios:
    """Tests for real-world usage scenarios."""

    def test_multi_turn_conversation_attributes(self):
        """Test attributes for multi-turn conversation."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "MiniMax-M2.5",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "My name is Alice."},
                {"role": "assistant", "content": "Hello Alice!"},
                {"role": "user", "content": "What is my name?"},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        # Should have 4 messages
        message_keys = [k for k in extra_attrs.keys() if "input_messages" in k]
        assert len(message_keys) >= 4

    def test_function_calling_flow(self):
        """Test function calling flow attributes."""
        from traceai_minimax._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor
        from traceai_minimax._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        # Initial request with tools
        req_extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "MiniMax-M2.5",
            "messages": [{"role": "user", "content": "What's the weather in Paris?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "location": {"type": "string"},
                            },
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        }
        req_attrs = dict(req_extractor.get_extra_attributes_from_request(request_params))
        assert req_attrs.get("minimax.tools_count") == 1

        # Response with tool call
        resp_extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "Paris"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        extra_attrs = dict(resp_extractor.get_extra_attributes(response, {}))
        assert extra_attrs.get("minimax.tool_calls_count") == 1
        assert extra_attrs.get("minimax.finish_reason") == "tool_calls"

    def test_streaming_response_extractor(self):
        """Test streaming response accumulation."""
        from traceai_minimax._response_attributes_extractor import _StreamingChatCompletionResponseExtractor

        extractor = _StreamingChatCompletionResponseExtractor()

        # Simulate streaming chunks
        chunks = [
            {"id": "chatcmpl-123", "model": "MiniMax-M2.5", "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
            {"id": "chatcmpl-123", "model": "MiniMax-M2.5", "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"id": "chatcmpl-123", "model": "MiniMax-M2.5", "choices": [{"delta": {"content": " world!"}, "finish_reason": None}]},
            {"id": "chatcmpl-123", "model": "MiniMax-M2.5", "choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
        ]

        for chunk in chunks:
            extractor.process_chunk(chunk)

        attrs = dict(extractor.get_attributes())
        extra_attrs = dict(extractor.get_extra_attributes({}))

        assert attrs.get("llm.model") == "MiniMax-M2.5"
        assert attrs.get("minimax.response_id") == "chatcmpl-123"
        assert attrs.get("llm.token_count.prompt") == 5
        assert attrs.get("llm.token_count.completion") == 3
        assert extra_attrs.get("minimax.finish_reason") == "stop"
