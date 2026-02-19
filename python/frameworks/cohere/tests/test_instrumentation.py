"""Tests for Cohere instrumentation."""
import json
from unittest.mock import MagicMock, patch

import pytest

from traceai_cohere import CohereInstrumentor


class TestCohereInstrumentor:
    """Tests for CohereInstrumentor."""

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        instrumentor = CohereInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "cohere >= 5.0.0" in deps


class TestChatRequestAttributesExtractor:
    """Tests for chat request attributes extraction."""

    def test_extract_chat_attributes(self):
        """Test extracting chat request attributes."""
        from traceai_cohere._request_attributes_extractor import _ChatRequestAttributesExtractor

        extractor = _ChatRequestAttributesExtractor()
        request_params = {
            "model": "command-r-plus",
            "message": "Hello!",
            "temperature": 0.7,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "LLM"
        assert attributes["llm.system"] == "cohere"
        assert attributes["llm.model"] == "command-r-plus"

    def test_extract_chat_with_history(self):
        """Test extracting chat with history."""
        from traceai_cohere._request_attributes_extractor import _ChatRequestAttributesExtractor

        extractor = _ChatRequestAttributesExtractor()
        request_params = {
            "model": "command-r-plus",
            "message": "What's my name?",
            "chat_history": [
                {"role": "USER", "message": "My name is Alice"},
                {"role": "CHATBOT", "message": "Hello Alice!"},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        # Should have messages from history
        assert any("input_messages" in k for k in extra_attrs.keys())

    def test_extract_preamble(self):
        """Test extracting preamble (system prompt)."""
        from traceai_cohere._request_attributes_extractor import _ChatRequestAttributesExtractor

        extractor = _ChatRequestAttributesExtractor()
        request_params = {
            "model": "command-r-plus",
            "message": "Hello",
            "preamble": "You are a helpful assistant.",
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert extra_attrs.get("llm.system_prompt") == "You are a helpful assistant."

    def test_extract_documents_count(self):
        """Test extracting documents count for RAG."""
        from traceai_cohere._request_attributes_extractor import _ChatRequestAttributesExtractor

        extractor = _ChatRequestAttributesExtractor()
        request_params = {
            "model": "command-r-plus",
            "message": "What is in the docs?",
            "documents": [
                {"text": "Doc 1"},
                {"text": "Doc 2"},
                {"text": "Doc 3"},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert extra_attrs.get("cohere.documents_count") == 3


class TestEmbedRequestAttributesExtractor:
    """Tests for embed request attributes extraction."""

    def test_extract_embed_attributes(self):
        """Test extracting embed request attributes."""
        from traceai_cohere._request_attributes_extractor import _EmbedRequestAttributesExtractor

        extractor = _EmbedRequestAttributesExtractor()
        request_params = {
            "model": "embed-english-v3.0",
            "texts": ["Hello", "World"],
            "input_type": "search_document",
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "EMBEDDING"
        assert attributes["llm.system"] == "cohere"
        assert attributes.get("cohere.input_type") == "search_document"

    def test_extract_texts_count(self):
        """Test extracting texts count."""
        from traceai_cohere._request_attributes_extractor import _EmbedRequestAttributesExtractor

        extractor = _EmbedRequestAttributesExtractor()
        request_params = {
            "model": "embed-english-v3.0",
            "texts": ["Hello", "World", "Test"],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert extra_attrs.get("cohere.texts_count") == 3


class TestRerankRequestAttributesExtractor:
    """Tests for rerank request attributes extraction."""

    def test_extract_rerank_attributes(self):
        """Test extracting rerank request attributes."""
        from traceai_cohere._request_attributes_extractor import _RerankRequestAttributesExtractor

        extractor = _RerankRequestAttributesExtractor()
        request_params = {
            "model": "rerank-english-v3.0",
            "query": "What is the capital of France?",
            "documents": ["Paris is the capital", "London is nice"],
            "top_n": 3,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "RERANKER"
        assert attributes["llm.system"] == "cohere"
        assert attributes.get("reranker.top_k") == 3

    def test_extract_rerank_query(self):
        """Test extracting rerank query."""
        from traceai_cohere._request_attributes_extractor import _RerankRequestAttributesExtractor

        extractor = _RerankRequestAttributesExtractor()
        request_params = {
            "model": "rerank-english-v3.0",
            "query": "What is ML?",
            "documents": ["doc1", "doc2"],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert extra_attrs.get("reranker.query") == "What is ML?"


class TestChatResponseAttributesExtractor:
    """Tests for chat response attributes extraction."""

    def test_extract_token_counts(self):
        """Test extracting token counts from response."""
        from traceai_cohere._response_attributes_extractor import _ChatResponseAttributesExtractor

        extractor = _ChatResponseAttributesExtractor()
        response = {
            "text": "Hello! How can I help?",
            "meta": {
                "billed_units": {
                    "input_tokens": 10,
                    "output_tokens": 15,
                }
            },
        }

        attributes = dict(extractor.get_attributes(response))

        assert attributes.get("llm.token_count.prompt") == 10
        assert attributes.get("llm.token_count.completion") == 15

    def test_extract_response_text(self):
        """Test extracting response text."""
        from traceai_cohere._response_attributes_extractor import _ChatResponseAttributesExtractor

        extractor = _ChatResponseAttributesExtractor()
        response = {
            "text": "Paris is the capital of France.",
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert extra_attrs.get("output.value") == "Paris is the capital of France."

    def test_extract_tool_calls(self):
        """Test extracting tool calls from response."""
        from traceai_cohere._response_attributes_extractor import _ChatResponseAttributesExtractor

        extractor = _ChatResponseAttributesExtractor()
        response = {
            "text": "",
            "tool_calls": [
                {"name": "get_weather", "parameters": {"location": "Paris"}},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert extra_attrs.get("cohere.tool_calls_count") == 1


class TestRerankResponseAttributesExtractor:
    """Tests for rerank response attributes extraction."""

    def test_extract_rerank_results(self):
        """Test extracting rerank results."""
        from traceai_cohere._response_attributes_extractor import _RerankResponseAttributesExtractor

        extractor = _RerankResponseAttributesExtractor()
        response = {
            "results": [
                {"index": 0, "relevance_score": 0.95},
                {"index": 2, "relevance_score": 0.82},
                {"index": 1, "relevance_score": 0.75},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert extra_attrs.get("cohere.results_count") == 3
        assert extra_attrs.get("cohere.rerank.0.relevance_score") == 0.95


class TestUtils:
    """Tests for utility functions."""

    def test_to_dict_with_dict(self):
        """Test _to_dict with dictionary input."""
        from traceai_cohere._utils import _to_dict

        input_dict = {"key": "value"}
        result = _to_dict(input_dict)

        assert result == input_dict

    def test_to_dict_with_none(self):
        """Test _to_dict with None input."""
        from traceai_cohere._utils import _to_dict

        result = _to_dict(None)

        assert result == {}


class TestRealWorldScenarios:
    """Tests for real-world RAG scenarios."""

    def test_rag_pipeline_attributes(self):
        """Test attributes for a typical RAG pipeline."""
        from traceai_cohere._request_attributes_extractor import (
            _ChatRequestAttributesExtractor,
            _EmbedRequestAttributesExtractor,
            _RerankRequestAttributesExtractor,
        )

        # Step 1: Embed query
        embed_extractor = _EmbedRequestAttributesExtractor()
        embed_params = {
            "model": "embed-english-v3.0",
            "texts": ["What is machine learning?"],
            "input_type": "search_query",
        }
        embed_attrs = dict(embed_extractor.get_attributes_from_request(embed_params))
        assert embed_attrs["fi.span.kind"] == "EMBEDDING"

        # Step 2: Rerank results
        rerank_extractor = _RerankRequestAttributesExtractor()
        rerank_params = {
            "model": "rerank-english-v3.0",
            "query": "What is machine learning?",
            "documents": ["ML is...", "AI is...", "Deep learning..."],
            "top_n": 2,
        }
        rerank_attrs = dict(rerank_extractor.get_attributes_from_request(rerank_params))
        assert rerank_attrs["fi.span.kind"] == "RERANKER"

        # Step 3: Generate with context
        chat_extractor = _ChatRequestAttributesExtractor()
        chat_params = {
            "model": "command-r-plus",
            "message": "What is machine learning?",
            "documents": [{"text": "ML is a subset of AI..."}],
        }
        chat_attrs = dict(chat_extractor.get_attributes_from_request(chat_params))
        assert chat_attrs["fi.span.kind"] == "LLM"

    def test_invocation_parameters(self):
        """Test extraction of invocation parameters."""
        from traceai_cohere._request_attributes_extractor import _ChatRequestAttributesExtractor

        extractor = _ChatRequestAttributesExtractor()
        request_params = {
            "model": "command-r-plus",
            "message": "Hello",
            "temperature": 0.7,
            "max_tokens": 500,
            "k": 0,
            "p": 0.9,
            "frequency_penalty": 0.5,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert "llm.invocation_parameters" in attributes
        params = json.loads(attributes["llm.invocation_parameters"])
        assert params["temperature"] == 0.7
        assert params["max_tokens"] == 500
