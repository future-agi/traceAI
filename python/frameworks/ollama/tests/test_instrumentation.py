"""Tests for Ollama instrumentation."""
import json
from unittest.mock import MagicMock, patch

import pytest


class TestOllamaInstrumentor:
    """Tests for OllamaInstrumentor."""

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        from traceai_ollama import OllamaInstrumentor

        instrumentor = OllamaInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "ollama >= 0.3.0" in deps


class TestRequestAttributesExtractor:
    """Tests for request attributes extraction."""

    def test_extract_chat_attributes(self):
        """Test extracting chat request attributes."""
        from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor

        extractor = _RequestAttributesExtractor()
        request_params = {
            "model": "llama3.2",
            "messages": [
                {"role": "user", "content": "Hello!"}
            ],
            "temperature": 0.7,
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "LLM"
        assert attributes["llm.system"] == "ollama"
        assert attributes["llm.model"] == "llama3.2"

    def test_extract_embed_attributes(self):
        """Test extracting embed request attributes."""
        from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor

        extractor = _RequestAttributesExtractor()
        request_params = {
            "model": "nomic-embed-text",
            "input": "Hello, world!",
            "_endpoint": "embed",
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        assert attributes["fi.span.kind"] == "EMBEDDING"
        assert attributes["llm.system"] == "ollama"

    def test_extract_messages(self):
        """Test extracting message attributes."""
        from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor

        extractor = _RequestAttributesExtractor()
        request_params = {
            "model": "llama3.2",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello!"},
            ],
        }

        extra_attrs = dict(extractor.get_extra_attributes_from_request(request_params))

        assert "llm.input_messages.0.message.role" in extra_attrs or any(
            "message.role" in k for k in extra_attrs.keys()
        )


class TestResponseAttributesExtractor:
    """Tests for response attributes extraction."""

    def test_extract_chat_response(self):
        """Test extracting chat response attributes."""
        from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor

        extractor = _ResponseAttributesExtractor()
        response = {
            "message": {"role": "assistant", "content": "Hello! How can I help?"},
            "prompt_eval_count": 10,
            "eval_count": 15,
            "total_duration": 1000000000,
        }

        attributes = dict(extractor.get_attributes(response))

        assert attributes.get("llm.token_count.prompt") == 10
        assert attributes.get("llm.token_count.completion") == 15

    def test_extract_generate_response(self):
        """Test extracting generate response attributes."""
        from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor

        extractor = _ResponseAttributesExtractor()
        response = {
            "response": "The quick brown fox",
            "prompt_eval_count": 5,
            "eval_count": 10,
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert extra_attrs.get("output.value") == "The quick brown fox"

    def test_extract_embed_response(self):
        """Test extracting embed response attributes."""
        from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor

        extractor = _ResponseAttributesExtractor()
        response = {
            "embedding": [0.1, 0.2, 0.3, 0.4, 0.5],
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert "5 dimensions" in extra_attrs.get("output.value", "")


class TestUtils:
    """Tests for utility functions."""

    def test_to_dict_with_dict(self):
        """Test _to_dict with dictionary input."""
        from traceai_ollama._utils import _to_dict

        input_dict = {"key": "value"}
        result = _to_dict(input_dict)

        assert result == input_dict

    def test_to_dict_with_none(self):
        """Test _to_dict with None input."""
        from traceai_ollama._utils import _to_dict

        result = _to_dict(None)

        assert result == {}

    def test_to_dict_with_object(self):
        """Test _to_dict with object having model_dump."""
        from traceai_ollama._utils import _to_dict

        class MockResponse:
            def model_dump(self):
                return {"key": "value"}

        result = _to_dict(MockResponse())

        assert result == {"key": "value"}


class TestWithSpan:
    """Tests for _WithSpan helper."""

    def test_finish_tracing(self):
        """Test finish_tracing sets attributes and ends span."""
        from traceai_ollama._with_span import _WithSpan

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        with_span = _WithSpan(
            span=mock_span,
            context_attributes={"ctx_key": "ctx_value"},
            extra_attributes={"extra_key": "extra_value"},
        )

        with_span.finish_tracing()

        mock_span.set_attribute.assert_called()
        mock_span.end.assert_called_once()


class TestRealWorldScenarios:
    """Tests for real-world usage scenarios."""

    def test_multi_turn_conversation_attributes(self):
        """Test attributes for multi-turn conversation."""
        from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor

        extractor = _RequestAttributesExtractor()
        request_params = {
            "model": "llama3.2",
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

    def test_model_parameters_extraction(self):
        """Test extraction of model parameters."""
        from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor

        extractor = _RequestAttributesExtractor()
        request_params = {
            "model": "llama3.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "num_predict": 100,
            "stop": ["END"],
        }

        attributes = dict(extractor.get_attributes_from_request(request_params))

        # Should have invocation parameters
        assert "llm.invocation_parameters" in attributes
        params = json.loads(attributes["llm.invocation_parameters"])
        assert params["temperature"] == 0.7
        assert params["top_p"] == 0.9

    def test_performance_metrics_extraction(self):
        """Test extraction of Ollama performance metrics."""
        from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor

        extractor = _ResponseAttributesExtractor()
        response = {
            "message": {"role": "assistant", "content": "Hello!"},
            "total_duration": 2000000000,
            "load_duration": 100000000,
            "prompt_eval_duration": 500000000,
            "eval_duration": 1400000000,
            "prompt_eval_count": 10,
            "eval_count": 5,
        }

        extra_attrs = dict(extractor.get_extra_attributes(response, {}))

        assert extra_attrs.get("ollama.total_duration_ns") == 2000000000
        assert extra_attrs.get("ollama.load_duration_ns") == 100000000
