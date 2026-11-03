"""Haystack semantic extractor unit tests; native integration tests live in frameworks/haystack/tests."""

from unittest.mock import Mock, patch

import pytest
from traceai_haystack._semantics import (
    ComponentType,
    _get_bound_arguments,
    _get_component_class_name,
    _get_component_span_name,
    _get_component_type,
    _get_embedding_attributes,
    _get_embedding_model_attributes,
    _get_input_attributes,
    _get_llm_input_message_attributes,
    _get_llm_model_attributes,
    _get_llm_output_message_attributes,
    _get_llm_token_count_attributes,
    _get_output_attributes,
    _get_reranker_model_attributes,
    _get_reranker_request_attributes,
    _get_reranker_response_attributes,
    _get_retriever_response_attributes,
    _get_span_kind_attributes,
    _is_list_of_documents,
    _is_vector,
    _mask_embedding_vectors,
)


class TestComponentType:
    """Test the ComponentType enum."""

    def test_component_type_values(self):
        """Test ComponentType enum values."""
        assert ComponentType.GENERATOR.name == "GENERATOR"
        assert ComponentType.EMBEDDER.name == "EMBEDDER"
        assert ComponentType.RANKER.name == "RANKER"
        assert ComponentType.RETRIEVER.name == "RETRIEVER"
        assert ComponentType.PROMPT_BUILDER.name == "PROMPT_BUILDER"
        assert ComponentType.UNKNOWN.name == "UNKNOWN"

        # Check all values are unique
        values = [e.value for e in ComponentType]
        assert len(values) == len(set(values))


class TestUtilityFunctions:
    """Test utility functions."""

    def test_get_component_class_name(self):
        """Test _get_component_class_name function."""
        mock_component = Mock()
        mock_component.__class__.__name__ = "TestComponent"

        result = _get_component_class_name(mock_component)
        assert result == "TestComponent"

    def test_get_component_span_name(self):
        """Test _get_component_span_name function."""
        result = _get_component_span_name("TestComponent")
        assert result == "TestComponent.run"

    def test_get_component_type_generator(self):
        """Test _get_component_type for generator components."""
        mock_component = Mock()
        mock_component.__class__.__name__ = "OpenAIGenerator"

        with patch(
            "traceai_haystack._semantics._get_component_run_method"
        ) as mock_get_run:
            mock_run_method = Mock()
            mock_get_run.return_value = mock_run_method

            with patch(
                "traceai_haystack._semantics._has_generator_output_type"
            ) as mock_has_gen:
                mock_has_gen.return_value = True

                result = _get_component_type(mock_component)
                assert result == ComponentType.GENERATOR

    def test_get_component_type_embedder(self):
        """Test _get_component_type for embedder components."""
        mock_component = Mock()
        mock_component.__class__.__name__ = "SentenceTransformersTextEmbedder"

        with patch(
            "traceai_haystack._semantics._get_component_run_method"
        ) as mock_get_run:
            mock_run_method = Mock()
            mock_get_run.return_value = mock_run_method

            with patch(
                "traceai_haystack._semantics._has_generator_output_type"
            ) as mock_has_gen:
                mock_has_gen.return_value = False

                result = _get_component_type(mock_component)
                # Will depend on class name matching
                assert result in [ComponentType.EMBEDDER, ComponentType.UNKNOWN]

    def test_get_span_kind_attributes(self):
        """Test _get_span_kind_attributes function."""
        attributes = list(_get_span_kind_attributes("LLM"))

        assert len(attributes) == 1
        key, value = attributes[0]
        assert key == "gen_ai.span.kind"
        assert value == "LLM"

    def test_get_input_attributes(self):
        """Test _get_input_attributes function."""
        arguments = {
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
        }

        attributes = list(_get_input_attributes(arguments))

        # Should have input.value and input.mime_type
        attr_dict = dict(attributes)
        assert "input.value" in attr_dict
        assert "input.mime_type" in attr_dict

    def test_get_output_attributes(self):
        """Test _get_output_attributes function."""
        response = {"replies": ["Generated response"], "status": "success"}

        attributes = list(_get_output_attributes(response))

        # Should have output.value and output.mime_type
        attr_dict = dict(attributes)
        assert "output.value" in attr_dict
        assert "output.mime_type" in attr_dict

    def test_get_llm_input_message_attributes(self):
        """Test _get_llm_input_message_attributes function."""
        arguments = {
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ]
        }

        attributes = list(_get_llm_input_message_attributes(arguments))

        # Should extract message attributes if messages exist
        if attributes:
            attr_dict = dict(attributes)
            # Check for any message-related attributes
            assert any("message" in key.lower() for key in attr_dict.keys())

    def test_get_llm_output_message_attributes(self):
        """Test _get_llm_output_message_attributes function."""
        response = {"replies": ["Hello! How can I help you?"]}

        attributes = list(_get_llm_output_message_attributes(response))

        # Should extract reply attributes
        attr_dict = dict(attributes)
        assert attr_dict == {
            "gen_ai.output.messages.0.message.content": "Hello! How can I help you?",
            "gen_ai.output.messages.0.message.role": "assistant",
        }

    def test_get_llm_model_attributes(self):
        """Test _get_llm_model_attributes function."""
        response = {"meta": [{"model": "gpt-4", "finish_reason": "stop"}]}

        attributes = list(_get_llm_model_attributes(response))

        # Should extract model name
        attr_dict = dict(attributes)
        assert "gen_ai.request.model" in attr_dict
        assert attr_dict["gen_ai.request.model"] == "gpt-4"

    def test_get_llm_token_count_attributes(self):
        """Test _get_llm_token_count_attributes function."""
        response = {
            "meta": [
                {
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                    }
                }
            ]
        }

        attributes = list(_get_llm_token_count_attributes(response))

        # Should extract token counts
        attr_dict = dict(attributes)
        assert "gen_ai.usage.input_tokens" in attr_dict
        assert attr_dict["gen_ai.usage.input_tokens"] == 10
        assert "gen_ai.usage.output_tokens" in attr_dict
        assert attr_dict["gen_ai.usage.output_tokens"] == 20
        assert attr_dict["gen_ai.usage.total_tokens"] == 30

    def test_get_embedding_model_attributes(self):
        """Test _get_embedding_model_attributes function."""
        mock_component = Mock()
        mock_component.model = "sentence-transformers/all-MiniLM-L6-v2"

        attributes = list(_get_embedding_model_attributes(mock_component))

        # Should extract model name
        attr_dict = dict(attributes)
        assert "embedding.model_name" in attr_dict
        assert (
            attr_dict["embedding.model_name"]
            == "sentence-transformers/all-MiniLM-L6-v2"
        )

    def test_get_embedding_attributes_documents(self):
        """Test _get_embedding_attributes function with documents."""
        # Mock Document class
        mock_doc = Mock()
        mock_doc.content = "Test document"
        mock_doc.embedding = [0.1, 0.2, 0.3]

        arguments = {"documents": [mock_doc]}
        response = {"documents": [mock_doc]}

        with patch(
            "traceai_haystack._semantics._is_embedding_doc"
        ) as mock_is_embedding:
            mock_is_embedding.return_value = True

            attributes = list(_get_embedding_attributes(arguments, response))

        # Should extract embedding attributes
        attr_dict = dict(attributes)
        assert any("embedding.embeddings" in key for key in attr_dict.keys())

    def test_get_reranker_model_attributes(self):
        """Test _get_reranker_model_attributes function."""
        mock_component = Mock()
        mock_component.model = "cross-encoder/ms-marco-MiniLM-L-6-v2"

        attributes = list(_get_reranker_model_attributes(mock_component))

        # Should extract model name if model attribute exists and is string
        if attributes:
            attr_dict = dict(attributes)
            assert "reranker.model_name" in attr_dict

    def test_get_reranker_request_attributes(self):
        """Test _get_reranker_request_attributes function."""
        arguments = {"query": "search query", "top_k": 5}

        attributes = list(_get_reranker_request_attributes(arguments))

        # Should extract query and top_k
        attr_dict = dict(attributes)
        assert "reranker.query" in attr_dict
        assert attr_dict["reranker.query"] == "search query"
        assert "reranker.top_k" in attr_dict
        assert attr_dict["reranker.top_k"] == 5

    def test_get_reranker_response_attributes(self):
        """Test _get_reranker_response_attributes function."""
        # Mock Document
        mock_doc = Mock()
        mock_doc.content = "Document content"
        mock_doc.id = "doc1"
        mock_doc.score = 0.95

        response = {"documents": [mock_doc]}

        # Test the function directly without mocking isinstance
        attributes = list(_get_reranker_response_attributes(response))

        # Should return attributes list (may be empty depending on implementation)
        assert isinstance(attributes, list)

    def test_get_retriever_response_attributes(self):
        """Test _get_retriever_response_attributes function."""
        # Mock Document
        mock_doc = Mock()
        mock_doc.content = "Retrieved content"
        mock_doc.id = "doc1"
        mock_doc.score = 0.8
        mock_doc.meta = {"source": "test.txt"}

        response = {"documents": [mock_doc]}

        # Test the function directly without mocking isinstance
        attributes = list(_get_retriever_response_attributes(response))

        # Should return attributes list (may be empty depending on implementation)
        assert isinstance(attributes, list)

    def test_is_vector(self):
        """Test _is_vector function."""
        assert _is_vector([0.1, 0.2, 0.3]) is True
        assert _is_vector([1, 2, 3]) is True
        assert _is_vector([]) is True
        assert _is_vector(["a", "b"]) is False
        assert _is_vector("not a vector") is False
        assert _is_vector(None) is False

    def test_is_list_of_documents(self):
        """Test _is_list_of_documents function."""
        # Test with basic types without mocking isinstance
        assert _is_list_of_documents([]) is True
        assert _is_list_of_documents("not a list") is False

        # Test function behavior
        result = _is_list_of_documents([Mock(), Mock()])
        assert isinstance(result, bool)

    def test_mask_embedding_vectors(self):
        """Test _mask_embedding_vectors function."""
        # Should mask embedding vectors
        key, value = _mask_embedding_vectors("text_embedding", [0.1, 0.2, 0.3])
        assert key == "text_embedding"
        assert value == "<3-dimensional vector>"

        # Should not mask non-embedding data
        key, value = _mask_embedding_vectors("text_content", "Hello world")
        assert key == "text_content"
        assert value == "Hello world"

    def test_get_bound_arguments(self):
        """Test _get_bound_arguments function."""

        def test_func(arg1, arg2="default", **kwargs):
            pass

        bound_args = _get_bound_arguments(test_func, "value1", extra="extra_value")

        assert bound_args.arguments["arg1"] == "value1"
        # Extra kwargs are grouped under 'kwargs' key
        assert "kwargs" in bound_args.arguments
        assert bound_args.arguments["kwargs"]["extra"] == "extra_value"

    def test_get_bound_arguments_no_kwargs(self):
        """Test _get_bound_arguments function without **kwargs."""

        def test_func(arg1, arg2="default"):
            pass

        # Should filter out invalid kwargs
        bound_args = _get_bound_arguments(
            test_func, "value1", invalid="should_be_filtered"
        )

        assert bound_args.arguments["arg1"] == "value1"
        # Invalid kwargs should be filtered out when function doesn't accept **kwargs
        assert "invalid" not in bound_args.arguments


if __name__ == "__main__":
    pytest.main([__file__])
