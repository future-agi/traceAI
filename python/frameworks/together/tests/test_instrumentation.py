"""Tests for Together AI instrumentation."""
import json
from unittest.mock import MagicMock, patch

import pytest

from traceai_together import TogetherInstrumentor


class TestTogetherInstrumentor:
    """Tests for TogetherInstrumentor."""

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        instrumentor = TogetherInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "together >= 1.0.0" in deps


class TestChatCompletionsRequestAttributesExtractor:
    """Tests for chat completions request attributes extraction."""

    def test_extract_chat_attributes(self):
        """Test extracting chat request attributes."""
        from traceai_together._request_attributes_extractor import _ChatCompletionsRequestAttributesExtractor

        extractor = _ChatCompletionsRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-3-8b-chat-hf",
            "messages": [
                {"role": "user", "content": "Hello!"}
            ],
            "temperature": 0.7,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "LLM"
        assert attributes["llm.system"] == "together"
        assert attributes["llm.model"] == "meta-llama/Llama-3-8b-chat-hf"

    def test_extract_chat_with_system_message(self):
        """Test extracting chat with system message."""
        from traceai_together._request_attributes_extractor import _ChatCompletionsRequestAttributesExtractor

        extractor = _ChatCompletionsRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-3-8b-chat-hf",
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
        from traceai_together._request_attributes_extractor import _ChatCompletionsRequestAttributesExtractor

        extractor = _ChatCompletionsRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-3-8b-chat-hf",
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

        # Tools are stored under llm.tools prefix
        tool_keys = [k for k in extra_attrs.keys() if "llm.tools" in k]
        assert len(tool_keys) >= 1


class TestCompletionsRequestAttributesExtractor:
    """Tests for completions request attributes extraction."""

    def test_extract_completions_attributes(self):
        """Test extracting completions request attributes."""
        from traceai_together._request_attributes_extractor import _CompletionsRequestAttributesExtractor

        extractor = _CompletionsRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-3-8b-hf",
            "prompt": "The capital of France is",
            "max_tokens": 100,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "LLM"
        assert attributes["llm.system"] == "together"
        assert attributes["llm.model"] == "meta-llama/Llama-3-8b-hf"


class TestEmbeddingsRequestAttributesExtractor:
    """Tests for embeddings request attributes extraction."""

    def test_extract_embeddings_attributes(self):
        """Test extracting embeddings request attributes."""
        from traceai_together._request_attributes_extractor import _EmbeddingsRequestAttributesExtractor

        extractor = _EmbeddingsRequestAttributesExtractor()
        request_params = {
            "model": "togethercomputer/m2-bert-80M-8k-retrieval",
            "input": ["Hello", "World"],
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "EMBEDDING"
        assert attributes["llm.system"] == "together"

    def test_extract_texts_count(self):
        """Test extracting texts count."""
        from traceai_together._request_attributes_extractor import _EmbeddingsRequestAttributesExtractor

        extractor = _EmbeddingsRequestAttributesExtractor()
        request_params = {
            "model": "togethercomputer/m2-bert-80M-8k-retrieval",
            "input": ["Hello", "World", "Test"],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert extra_attrs.get("together.texts_count") == 3


class TestChatCompletionsResponseAttributesExtractor:
    """Tests for chat completions response attributes extraction."""

    def test_extract_token_counts(self):
        """Test extracting token counts from response."""
        from traceai_together._response_attributes_extractor import _ChatCompletionsResponseAttributesExtractor

        extractor = _ChatCompletionsResponseAttributesExtractor()
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
        from traceai_together._response_attributes_extractor import _ChatCompletionsResponseAttributesExtractor

        extractor = _ChatCompletionsResponseAttributesExtractor()
        response = {
            "choices": [
                {"message": {"role": "assistant", "content": "Paris is the capital."}}
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert extra_attrs.get("output.value") == "Paris is the capital."

    def test_extract_tool_calls(self):
        """Test extracting tool calls from response."""
        from traceai_together._response_attributes_extractor import _ChatCompletionsResponseAttributesExtractor

        extractor = _ChatCompletionsResponseAttributesExtractor()
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

        # Tool calls are stored with message.tool_calls prefix
        tool_call_keys = [k for k in extra_attrs.keys() if "tool_calls" in k]
        assert len(tool_call_keys) >= 1


class TestUtils:
    """Tests for utility functions."""

    def test_to_dict_with_dict(self):
        """Test _to_dict with dictionary input."""
        from traceai_together._utils import _to_dict

        input_dict = {"key": "value"}
        result = _to_dict(input_dict)

        assert result == input_dict

    def test_to_dict_with_none(self):
        """Test _to_dict with None input."""
        from traceai_together._utils import _to_dict

        result = _to_dict(None)

        assert result == {}

    def test_to_dict_with_object(self):
        """Test _to_dict with object having model_dump."""
        from traceai_together._utils import _to_dict

        class MockResponse:
            def model_dump(self):
                return {"key": "value"}

        result = _to_dict(MockResponse())

        assert result == {"key": "value"}


class TestRealWorldScenarios:
    """Tests for real-world usage scenarios."""

    def test_multi_turn_conversation_attributes(self):
        """Test attributes for multi-turn conversation."""
        from traceai_together._request_attributes_extractor import _ChatCompletionsRequestAttributesExtractor

        extractor = _ChatCompletionsRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-3-8b-chat-hf",
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

    def test_invocation_parameters(self):
        """Test extraction of invocation parameters."""
        from traceai_together._request_attributes_extractor import _ChatCompletionsRequestAttributesExtractor

        extractor = _ChatCompletionsRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-3-8b-chat-hf",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "max_tokens": 500,
            "top_p": 0.9,
            "top_k": 40,
            "stop": ["END"],
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert "llm.invocation_parameters" in attributes
        params = json.loads(attributes["llm.invocation_parameters"])
        assert params["temperature"] == 0.7
        assert params["max_tokens"] == 500

    def test_embedding_pipeline(self):
        """Test attributes for embedding generation."""
        from traceai_together._request_attributes_extractor import _EmbeddingsRequestAttributesExtractor
        from traceai_together._response_attributes_extractor import _EmbeddingsResponseAttributesExtractor

        # Request
        req_extractor = _EmbeddingsRequestAttributesExtractor()
        request_params = {
            "model": "togethercomputer/m2-bert-80M-8k-retrieval",
            "input": ["Document 1", "Document 2", "Document 3"],
        }
        req_attrs = dict(req_extractor.get_attributes_from_request(request_params))
        assert req_attrs["fi.span.kind"] == "EMBEDDING"

        # Response
        resp_extractor = _EmbeddingsResponseAttributesExtractor()
        response = {
            "data": [
                {"embedding": [0.1] * 768, "index": 0},
                {"embedding": [0.2] * 768, "index": 1},
                {"embedding": [0.3] * 768, "index": 2},
            ],
            "usage": {"total_tokens": 10},
        }
        resp_attrs = dict(resp_extractor.get_extra_attributes(response, {}))
        assert resp_attrs.get("together.embeddings_count") == 3
        assert resp_attrs.get("together.embedding_dimensions") == 768
