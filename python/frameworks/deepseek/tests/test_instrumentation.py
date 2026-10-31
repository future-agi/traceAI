"""Tests for DeepSeek instrumentation."""
import json
from unittest.mock import MagicMock, patch

import pytest

from traceai_deepseek import DeepSeekInstrumentor


class TestDeepSeekInstrumentor:
    """Tests for DeepSeekInstrumentor."""

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        instrumentor = DeepSeekInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "openai >= 1.0.0" in deps


class TestChatCompletionRequestAttributesExtractor:
    """Tests for chat completion request attributes extraction."""

    def test_extract_chat_attributes(self):
        """Test extracting chat request attributes."""
        from traceai_deepseek._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "user", "content": "Hello!"}
            ],
            "temperature": 0.7,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["gen_ai.span.kind"] == "LLM"
        assert attributes["gen_ai.provider.name"] == "deepseek"
        assert attributes["gen_ai.request.model"] == "deepseek-chat"

    def test_extract_chat_with_system_message(self):
        """Test extracting chat with system message."""
        from traceai_deepseek._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        # Should have messages
        assert any("gen_ai.input.messages" in k for k in extra_attrs.keys())

    def test_extract_tools(self):
        """Test extracting tools from request."""
        from traceai_deepseek._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "deepseek-chat",
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

        assert extra_attrs.get("deepseek.tools_count") == 1

    def test_extract_invocation_parameters(self):
        """Test extracting invocation parameters."""
        from traceai_deepseek._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "max_tokens": 1024,
            "top_p": 0.9,
            "frequency_penalty": 0.5,
            "presence_penalty": 0.5,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert "gen_ai.request.parameters" in attributes
        params = json.loads(attributes["gen_ai.request.parameters"])
        assert params["temperature"] == 0.7
        assert params["max_tokens"] == 1024


class TestChatCompletionResponseAttributesExtractor:
    """Tests for chat completion response attributes extraction."""

    def test_extract_token_counts(self):
        """Test extracting token counts from response."""
        from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

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

        assert attributes.get("gen_ai.usage.input_tokens") == 10
        assert attributes.get("gen_ai.usage.output_tokens") == 15

    def test_extract_response_content(self):
        """Test extracting response content."""
        from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "choices": [
                {"message": {"role": "assistant", "content": "Paris is the capital."}}
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        # output.value is the serialized provider response; message text is
        # also retained separately for display and content-level masking.
        assert json.loads(extra_attrs["output.value"]) == response
        assert extra_attrs["gen_ai.output.messages.0.message.content"] == "Paris is the capital."
        assert extra_attrs["gen_ai.output.messages.0.message.role"] == "assistant"

    def test_extract_tool_calls(self):
        """Test extracting tool calls from response."""
        from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

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

        assert extra_attrs.get("deepseek.tool_calls_count") == 1

    def test_extract_prompt_cache_metrics(self):
        """Test extracting prompt cache metrics (DeepSeek-specific)."""
        from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "choices": [
                {"message": {"role": "assistant", "content": "Hello!"}}
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            },
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        attributes = dict(extractor.get_attributes(response))
        assert attributes["deepseek.prompt_cache_hit_tokens"] == 80
        assert attributes["deepseek.prompt_cache_miss_tokens"] == 20
        assert json.loads(extra_attrs["output.value"]) == response

    def test_extract_reasoning_content_r1(self):
        """Test extracting reasoning content for R1 models."""
        from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "36",
                        "reasoning_content": "15% of 240 = 0.15 * 240 = 36",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 50,
                "completion_tokens_details": {
                    "reasoning_tokens": 30,
                },
            },
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        attributes = dict(extractor.get_attributes(response))
        assert extra_attrs["deepseek.reasoning_content"] == "15% of 240 = 0.15 * 240 = 36"
        assert attributes["deepseek.reasoning_tokens"] == 30
        assert json.loads(extra_attrs["output.value"]) == response


class TestUtils:
    """Tests for utility functions."""

    def test_to_dict_with_dict(self):
        """Test _to_dict with dictionary input."""
        from traceai_deepseek._utils import _to_dict

        input_dict = {"key": "value"}
        result = _to_dict(input_dict)

        assert result == input_dict

    def test_to_dict_with_none(self):
        """Test _to_dict with None input."""
        from traceai_deepseek._utils import _to_dict

        result = _to_dict(None)

        assert result == {}

    def test_to_dict_with_object(self):
        """Test _to_dict with object having model_dump."""
        from traceai_deepseek._utils import _to_dict

        class MockResponse:
            def model_dump(self):
                return {"key": "value"}

        result = _to_dict(MockResponse())

        assert result == {"key": "value"}

    def test_is_deepseek_client_with_deepseek_url(self):
        """Test is_deepseek_client with DeepSeek URL."""
        from traceai_deepseek._utils import is_deepseek_client

        mock_client = MagicMock()
        mock_client.base_url = "https://api.deepseek.com/v1"

        assert is_deepseek_client(mock_client) is True

    def test_is_deepseek_client_with_openai_url(self):
        """Test is_deepseek_client with OpenAI URL."""
        from traceai_deepseek._utils import is_deepseek_client

        mock_client = MagicMock()
        mock_client.base_url = "https://api.openai.com/v1"

        assert is_deepseek_client(mock_client) is False


class TestRealWorldScenarios:
    """Tests for real-world usage scenarios."""

    def test_multi_turn_conversation_attributes(self):
        """Test attributes for multi-turn conversation."""
        from traceai_deepseek._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "My name is Alice."},
                {"role": "assistant", "content": "Hello Alice!"},
                {"role": "user", "content": "What is my name?"},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        for index, message in enumerate(request_params["messages"]):
            prefix = f"gen_ai.input.messages.{index}"
            assert extra_attrs[f"{prefix}.message.role"] == message["role"]
            assert extra_attrs[f"{prefix}.message.content"] == message["content"]

    def test_deepseek_reasoner_attributes(self):
        """Test attributes for DeepSeek Reasoner (R1) model."""
        from traceai_deepseek._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor
        from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        # Request
        req_extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "deepseek-reasoner",
            "messages": [
                {"role": "user", "content": "What is 15% of 240?"}
            ],
        }
        req_attrs = dict(req_extractor.get_attributes_from_request(request_params))
        assert req_attrs["gen_ai.request.model"] == "deepseek-reasoner"

        # Response with reasoning
        resp_extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The answer is 36.",
                        "reasoning_content": "To find 15% of 240:\n1. Convert 15% to decimal: 0.15\n2. Multiply: 0.15 × 240 = 36",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 60,
                "total_tokens": 75,
                "completion_tokens_details": {
                    "reasoning_tokens": 40,
                },
            },
        }
        resp_attrs = dict(resp_extractor.get_attributes(response))
        extra_attrs = dict(resp_extractor.get_extra_attributes(response, {}))

        assert extra_attrs["deepseek.reasoning_content"] == response["choices"][0]["message"]["reasoning_content"]
        assert resp_attrs["deepseek.reasoning_tokens"] == 40
        assert json.loads(extra_attrs["output.value"]) == response

    def test_function_calling_flow(self):
        """Test function calling flow attributes."""
        from traceai_deepseek._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor
        from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        # Initial request with tools
        req_extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "deepseek-chat",
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
        assert req_attrs.get("deepseek.tools_count") == 1

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
        assert extra_attrs.get("deepseek.tool_calls_count") == 1
        assert extra_attrs.get("deepseek.finish_reason") == "tool_calls"

    def test_prompt_caching_scenario(self):
        """Test prompt caching scenario with repeated system prompts."""
        from traceai_deepseek._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()

        # First request - no cache
        response1 = {
            "choices": [{"message": {"role": "assistant", "content": "Hi!"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 100,
            },
        }
        attrs1 = dict(extractor.get_extra_attributes(response1, {}))
        raw_output1 = json.loads(attrs1["output.value"])
        assert raw_output1["usage"]["prompt_cache_miss_tokens"] == 100
        assert raw_output1["usage"]["prompt_cache_hit_tokens"] == 0

        # Second request - cache hit
        response2 = {
            "choices": [{"message": {"role": "assistant", "content": "Hello again!"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 8,
                "prompt_cache_hit_tokens": 90,
                "prompt_cache_miss_tokens": 10,
            },
        }
        attrs2 = dict(extractor.get_extra_attributes(response2, {}))
        raw_output2 = json.loads(attrs2["output.value"])
        assert raw_output2["usage"]["prompt_cache_hit_tokens"] == 90
        assert raw_output2["usage"]["prompt_cache_miss_tokens"] == 10
