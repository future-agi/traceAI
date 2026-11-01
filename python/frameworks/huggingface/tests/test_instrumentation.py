"""Tests for HuggingFace instrumentation."""
import json
from unittest.mock import MagicMock, patch

import pytest

from traceai_huggingface import HuggingFaceInstrumentor


class TestHuggingFaceInstrumentor:
    """Tests for HuggingFaceInstrumentor."""

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        instrumentor = HuggingFaceInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "huggingface-hub >= 0.20.0" in deps


class TestTextGenerationRequestAttributesExtractor:
    """Tests for text generation request attributes extraction."""

    def test_extract_text_generation_attributes(self):
        """Test extracting text generation request attributes."""
        from traceai_huggingface._request_attributes_extractor import _TextGenerationRequestAttributesExtractor

        extractor = _TextGenerationRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-2-7b-chat-hf",
            "prompt": "The capital of France is",
            "max_new_tokens": 100,
            "temperature": 0.7,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["gen_ai.span.kind"] == "LLM"
        assert attributes["gen_ai.provider.name"] == "huggingface"
        assert attributes["gen_ai.request.model"] == "meta-llama/Llama-2-7b-chat-hf"

    def test_extract_generation_parameters(self):
        """Test extracting generation parameters."""
        from traceai_huggingface._request_attributes_extractor import _TextGenerationRequestAttributesExtractor

        extractor = _TextGenerationRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-2-7b-chat-hf",
            "prompt": "Hello",
            "temperature": 0.8,
            "top_p": 0.9,
            "top_k": 50,
            "max_new_tokens": 200,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert "gen_ai.request.parameters" in attributes
        params = json.loads(attributes["gen_ai.request.parameters"])
        assert params["temperature"] == 0.8
        assert params["max_new_tokens"] == 200


class TestChatCompletionRequestAttributesExtractor:
    """Tests for chat completion request attributes extraction."""

    def test_extract_chat_attributes(self):
        """Test extracting chat completion request attributes."""
        from traceai_huggingface._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-2-7b-chat-hf",
            "messages": [
                {"role": "user", "content": "Hello!"}
            ],
            "temperature": 0.7,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["gen_ai.span.kind"] == "LLM"
        assert attributes["gen_ai.provider.name"] == "huggingface"

    def test_extract_messages(self):
        """Test extracting messages from request."""
        from traceai_huggingface._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-2-7b-chat-hf",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello!"},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        # Should have messages
        assert any("gen_ai.input.messages" in k for k in extra_attrs.keys())

    def test_extract_tools(self):
        """Test extracting tools from request."""
        from traceai_huggingface._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-2-7b-chat-hf",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather info",
                    },
                }
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert extra_attrs.get("huggingface.tools_count") == 1


class TestFeatureExtractionRequestAttributesExtractor:
    """Tests for feature extraction request attributes extraction."""

    def test_extract_embedding_attributes(self):
        """Test extracting feature extraction request attributes."""
        from traceai_huggingface._request_attributes_extractor import _FeatureExtractionRequestAttributesExtractor

        extractor = _FeatureExtractionRequestAttributesExtractor()
        request_params = {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "inputs": "Hello, world!",
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["gen_ai.span.kind"] == "EMBEDDING"
        assert attributes["gen_ai.provider.name"] == "huggingface"

    def test_extract_multiple_texts(self):
        """Test extracting multiple texts count."""
        from traceai_huggingface._request_attributes_extractor import _FeatureExtractionRequestAttributesExtractor

        extractor = _FeatureExtractionRequestAttributesExtractor()
        request_params = {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "text": ["Hello", "World", "Test"],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert json.loads(extra_attrs["input.value"]) == request_params["text"]
        assert extra_attrs["huggingface.texts_count"] == 3


class TestTextGenerationResponseAttributesExtractor:
    """Tests for text generation response attributes extraction."""

    def test_extract_generated_text(self):
        """Test extracting generated text from response."""
        from traceai_huggingface._response_attributes_extractor import _TextGenerationResponseAttributesExtractor

        extractor = _TextGenerationResponseAttributesExtractor()
        response = {
            "generated_text": "Paris, the capital of France.",
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        # output.value is the serialized provider response; message text is
        # also retained separately for display and content-level masking.
        assert json.loads(extra_attrs["output.value"]) == response
        assert extra_attrs["gen_ai.output.messages.0.message.content"] == "Paris, the capital of France."
        assert extra_attrs["gen_ai.output.messages.0.message.role"] == "assistant"

    def test_extract_token_counts(self):
        """Test extracting token counts from response."""
        from traceai_huggingface._response_attributes_extractor import _TextGenerationResponseAttributesExtractor

        extractor = _TextGenerationResponseAttributesExtractor()
        response = {
            "generated_text": "Hello!",
            "details": {
                "generated_tokens": 15,
                "finish_reason": "stop",
            },
        }

        attributes = dict(extractor.get_attributes(response))

        assert attributes.get("gen_ai.usage.output_tokens") == 15


class TestChatCompletionResponseAttributesExtractor:
    """Tests for chat completion response attributes extraction."""

    def test_extract_chat_response(self):
        """Test extracting chat response attributes."""
        from traceai_huggingface._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

        extractor = _ChatCompletionResponseAttributesExtractor()
        response = {
            "choices": [
                {"message": {"role": "assistant", "content": "Hello! How can I help?"}}
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

    def test_extract_tool_calls(self):
        """Test extracting tool calls from response."""
        from traceai_huggingface._response_attributes_extractor import _ChatCompletionResponseAttributesExtractor

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

        # Tool calls stored under choices prefix
        tool_call_keys = [k for k in extra_attrs.keys() if "tool_calls" in k]
        assert len(tool_call_keys) >= 1


class TestFeatureExtractionResponseAttributesExtractor:
    """Tests for feature extraction response attributes extraction."""

    def test_extract_embedding_dimensions(self):
        """Test extracting embedding dimensions."""
        from traceai_huggingface._response_attributes_extractor import _FeatureExtractionResponseAttributesExtractor

        extractor = _FeatureExtractionResponseAttributesExtractor()
        response = [[0.1, 0.2, 0.3, 0.4, 0.5] * 76 + [0.1, 0.2, 0.3, 0.4]]  # 384 dimensions

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert extra_attrs.get("huggingface.embedding_dimensions") == 384


class TestUtils:
    """Tests for utility functions."""

    def test_to_dict_with_dict(self):
        """Test _to_dict with dictionary input."""
        from traceai_huggingface._utils import _to_dict

        input_dict = {"key": "value"}
        result = _to_dict(input_dict)

        assert result == input_dict

    def test_to_dict_with_none(self):
        """Test _to_dict with None input."""
        from traceai_huggingface._utils import _to_dict

        result = _to_dict(None)

        assert result == {}


class TestRealWorldScenarios:
    """Tests for real-world usage scenarios."""

    def test_semantic_search_pipeline(self):
        """Test attributes for semantic search pipeline."""
        from traceai_huggingface._request_attributes_extractor import _FeatureExtractionRequestAttributesExtractor
        from traceai_huggingface._response_attributes_extractor import _FeatureExtractionResponseAttributesExtractor

        # Embed query
        req_extractor = _FeatureExtractionRequestAttributesExtractor()
        request_params = {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "inputs": "What is machine learning?",
        }
        req_attrs = dict(req_extractor.get_attributes_from_request(request_params))
        assert req_attrs["gen_ai.span.kind"] == "EMBEDDING"

        # Response
        resp_extractor = _FeatureExtractionResponseAttributesExtractor()
        response = [[0.1] * 384]  # 384-dim embedding
        resp_attrs = dict(resp_extractor.get_extra_attributes(response, {}))
        assert resp_attrs.get("huggingface.embedding_dimensions") == 384

    def test_rag_pipeline_attributes(self):
        """Test attributes for a RAG pipeline."""
        from traceai_huggingface._request_attributes_extractor import (
            _FeatureExtractionRequestAttributesExtractor,
            _ChatCompletionRequestAttributesExtractor,
        )

        # Step 1: Embed documents
        embed_extractor = _FeatureExtractionRequestAttributesExtractor()
        embed_params = {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "inputs": ["Doc 1", "Doc 2", "Doc 3"],
        }
        embed_attrs = dict(embed_extractor.get_attributes_from_request(embed_params))
        assert embed_attrs["gen_ai.span.kind"] == "EMBEDDING"

        # Step 2: Generate with context
        chat_extractor = _ChatCompletionRequestAttributesExtractor()
        chat_params = {
            "model": "meta-llama/Llama-2-7b-chat-hf",
            "messages": [
                {"role": "system", "content": "Answer based on the context: Doc 1, Doc 2"},
                {"role": "user", "content": "What is in the docs?"},
            ],
        }
        chat_attrs = dict(chat_extractor.get_attributes_from_request(chat_params))
        assert chat_attrs["gen_ai.span.kind"] == "LLM"

    def test_multi_turn_conversation_attributes(self):
        """Test attributes for multi-turn conversation."""
        from traceai_huggingface._request_attributes_extractor import _ChatCompletionRequestAttributesExtractor

        extractor = _ChatCompletionRequestAttributesExtractor()
        request_params = {
            "model": "meta-llama/Llama-2-7b-chat-hf",
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
