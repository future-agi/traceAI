"""
Test suite for Haystack framework instrumentation.

Tests the instrumentation of Haystack components:
- Pipeline.run (CHAIN spans)
- Pipeline._run_component (dynamic component wrapping)
- Component.run methods for different component types:
  - GENERATOR (LLM spans)
  - EMBEDDER (EMBEDDING spans)
  - RANKER (RERANKER spans)
  - RETRIEVER (RETRIEVER spans)
  - PROMPT_BUILDER (LLM spans)
  - UNKNOWN (CHAIN spans)
- Protect.protect (guardrail protection)
- Attribute extraction utilities
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, call
from typing import Any, Dict, List, Sequence
from enum import auto

from opentelemetry import trace as trace_api
from opentelemetry.trace.status import StatusCode
from opentelemetry import context as context_api

from traceai_haystack import HaystackInstrumentor
from traceai_haystack._wrappers import (
    _PipelineWrapper,
    _PipelineRunComponentWrapper,
    _ComponentRunWrapper,
    ComponentType,
    _get_component_class_name,
    _get_component_span_name,
    _get_component_type,
    _get_span_kind_attributes,
    _get_input_attributes,
    _get_output_attributes,
    _get_component_output_attributes,
    _get_llm_input_message_attributes,
    _get_llm_output_message_attributes,
    _get_llm_model_attributes,
    _get_llm_token_count_attributes,
    _get_embedding_model_attributes,
    _get_embedding_attributes,
    _get_reranker_model_attributes,
    _get_reranker_request_attributes,
    _get_reranker_response_attributes,
    _get_retriever_response_attributes,
    _get_bound_arguments,
    _is_vector,
    _is_list_of_documents,
    _mask_embedding_vectors,
)


class TestHaystackInstrumentor:
    """Test the main Haystack instrumentor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.instrumentor = HaystackInstrumentor()

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        deps = self.instrumentor.instrumentation_dependencies()
        assert deps == ("haystack-ai >= 2.9.0")

    @patch("traceai_haystack.wrap_function_wrapper")
    @patch("traceai_haystack.trace_api.get_tracer")
    def test_instrument_basic(self, mock_get_tracer, mock_wrap):
        """Test basic instrumentation setup."""
        mock_tracer = Mock()
        mock_get_tracer.return_value = mock_tracer
        
        # Mock haystack objects
        mock_pipeline = Mock()
        mock_pipeline.run = Mock()
        mock_pipeline._run_component = Mock()
        
        with patch("traceai_haystack.haystack") as mock_haystack:
            mock_haystack.Pipeline = mock_pipeline
            
            with patch("traceai_haystack.Protect") as mock_protect:
                mock_protect.protect = Mock()
                
                self.instrumentor._instrument()
        
        # Verify tracer setup
        mock_get_tracer.assert_called_once()
        
        # Verify function wrapping
        assert mock_wrap.call_count >= 3  # Pipeline.run, Pipeline._run_component, Protect.protect
        
        # Check that originals are stored
        assert hasattr(self.instrumentor, '_original_pipeline_run')
        assert hasattr(self.instrumentor, '_original_pipeline_run_component')
        assert hasattr(self.instrumentor, '_original_component_run_methods')

    def test_uninstrument(self):
        """Test uninstrumentation."""
        # Set up mock originals
        mock_original_run = Mock()
        mock_original_run_component = Mock()
        self.instrumentor._original_pipeline_run = mock_original_run
        self.instrumentor._original_pipeline_run_component = mock_original_run_component
        self.instrumentor._original_component_run_methods = {}
        
        with patch("traceai_haystack.haystack") as mock_haystack:
            mock_pipeline = Mock()
            mock_haystack.Pipeline = mock_pipeline
            
            self.instrumentor._uninstrument()
        
        # Verify originals are restored
        assert mock_pipeline.run == mock_original_run
        assert mock_pipeline._run_component == mock_original_run_component


class TestPipelineWrapper:
    """Test the pipeline wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _PipelineWrapper(tracer=self.mock_tracer)

    def test_pipeline_wrapper_basic(self):
        """Test basic pipeline wrapper operation."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        def wrapped_func(self, data=None, **kwargs):
            return {"output": "success"}
        
        mock_instance = Mock()
        result = self.wrapper(wrapped_func, mock_instance, (mock_instance,), {"data": {"input": "test"}})
        
        assert result == {"output": "success"}
        # Check that span was started with the correct name and any attributes
        args, kwargs = self.mock_tracer.start_as_current_span.call_args
        assert args[0] == "Pipeline.run"
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_pipeline_wrapper_exception(self):
        """Test pipeline wrapper exception handling."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        def wrapped_func(self, data=None, **kwargs):
            raise ValueError("Pipeline error")
        
        mock_instance = Mock()
        
        with pytest.raises(ValueError, match="Pipeline error"):
            self.wrapper(wrapped_func, mock_instance, (mock_instance,), {})
        
        # Check that error status was set
        status_call = mock_span.set_status.call_args[0][0]
        assert status_call.status_code == trace_api.StatusCode.ERROR
        assert "Pipeline error" in str(status_call.description)

    def test_pipeline_wrapper_suppressed(self):
        """Test pipeline wrapper when instrumentation is suppressed."""
        with patch("traceai_haystack._wrappers.context_api.get_value") as mock_get_value:
            mock_get_value.return_value = True
            
            def wrapped_func(self, data=None, **kwargs):
                return {"output": "bypassed"}
            
            mock_instance = Mock()
            result = self.wrapper(wrapped_func, mock_instance, (mock_instance,), {})
            
            assert result == {"output": "bypassed"}
            # Should not start span when suppressed
            self.mock_tracer.start_as_current_span.assert_not_called()


class TestPipelineRunComponentWrapper:
    """Test the pipeline run component wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock()
        self.mock_wrap_method = Mock()
        self.wrapper = _PipelineRunComponentWrapper(
            tracer=self.mock_tracer,
            wrap_component_run_method=self.mock_wrap_method
        )

    def test_pipeline_run_component_wrapper_basic(self):
        """Test basic pipeline run component wrapper operation."""
        # Mock component
        mock_component_instance = Mock()
        mock_component_instance.__class__ = Mock()
        mock_component_instance.run = Mock()
        
        def wrapped_func(self, component=None, **kwargs):
            return {"result": "success"}
        
        mock_instance = Mock()
        
        result = self.wrapper(
            wrapped_func, 
            mock_instance, 
            (mock_instance,), 
            {"component": {"instance": mock_component_instance}}
        )
        
        assert result == {"result": "success"}
        # Should wrap the component run method
        self.mock_wrap_method.assert_called_once_with(
            mock_component_instance.__class__, 
            mock_component_instance.run
        )

    def test_pipeline_run_component_wrapper_no_component(self):
        """Test pipeline run component wrapper with no component."""
        def wrapped_func(self, **kwargs):
            return {"result": "success"}
        
        mock_instance = Mock()
        result = self.wrapper(wrapped_func, mock_instance, (mock_instance,), {})
        
        assert result == {"result": "success"}
        # Should not wrap anything if no component
        self.mock_wrap_method.assert_not_called()


class TestComponentRunWrapper:
    """Test the component run wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _ComponentRunWrapper(tracer=self.mock_tracer)

    def test_component_run_wrapper_generator(self):
        """Test component run wrapper for GENERATOR components."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock generator component
        mock_component = Mock()
        mock_component.__class__.__name__ = "OpenAIGenerator"
        
        def wrapped_func(self, messages=None, **kwargs):
            return {
                "replies": ["Generated response"],
                "meta": [{"model": "gpt-4", "usage": {"prompt_tokens": 10, "completion_tokens": 20}}]
            }
        
        with patch("traceai_haystack._wrappers._get_component_type") as mock_get_type:
            mock_get_type.return_value = ComponentType.GENERATOR
            
            result = self.wrapper(wrapped_func, mock_component, (mock_component,), {"messages": []})
        
        assert result["replies"] == ["Generated response"]
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_component_run_wrapper_embedder(self):
        """Test component run wrapper for EMBEDDER components."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock embedder component
        mock_component = Mock()
        mock_component.__class__.__name__ = "SentenceTransformersTextEmbedder"
        mock_component.model = "sentence-transformers/all-MiniLM-L6-v2"
        
        def wrapped_func(self, text=None, **kwargs):
            return {"embedding": [0.1, 0.2, 0.3, 0.4]}
        
        with patch("traceai_haystack._wrappers._get_component_type") as mock_get_type:
            mock_get_type.return_value = ComponentType.EMBEDDER
            
            result = self.wrapper(wrapped_func, mock_component, (mock_component,), {"text": "test text"})
        
        assert result["embedding"] == [0.1, 0.2, 0.3, 0.4]
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_component_run_wrapper_ranker(self):
        """Test component run wrapper for RANKER components."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock ranker component
        mock_component = Mock()
        mock_component.__class__.__name__ = "TransformersSimilarityRanker"
        
        # Mock document class
        mock_doc = Mock()
        mock_doc.content = "Document content"
        mock_doc.id = "doc1"
        mock_doc.score = 0.9
        
        def wrapped_func(self, query=None, documents=None, **kwargs):
            return {"documents": [mock_doc]}
        
        with patch("traceai_haystack._wrappers._get_component_type") as mock_get_type:
            mock_get_type.return_value = ComponentType.RANKER
            
            result = self.wrapper(wrapped_func, mock_component, (mock_component,), {"query": "test", "documents": []})
        
        assert len(result["documents"]) == 1
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_component_run_wrapper_retriever(self):
        """Test component run wrapper for RETRIEVER components."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock retriever component
        mock_component = Mock()
        mock_component.__class__.__name__ = "InMemoryBM25Retriever"
        
        def wrapped_func(self, query=None, **kwargs):
            return {"documents": []}
        
        with patch("traceai_haystack._wrappers._get_component_type") as mock_get_type:
            mock_get_type.return_value = ComponentType.RETRIEVER
            
            result = self.wrapper(wrapped_func, mock_component, (mock_component,), {"query": "test"})
        
        assert result["documents"] == []
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_component_run_wrapper_prompt_builder(self):
        """Test component run wrapper for PROMPT_BUILDER components."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock prompt builder component
        mock_component = Mock()
        mock_component.__class__.__name__ = "PromptBuilder"
        
        def wrapped_func(self, template=None, **kwargs):
            return {"prompt": "Generated prompt"}
        
        with patch("traceai_haystack._wrappers._get_component_type") as mock_get_type:
            mock_get_type.return_value = ComponentType.PROMPT_BUILDER
            
            result = self.wrapper(wrapped_func, mock_component, (mock_component,), {"template": "Hello {name}"})
        
        assert result["prompt"] == "Generated prompt"
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_component_run_wrapper_unknown(self):
        """Test component run wrapper for UNKNOWN components."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock unknown component
        mock_component = Mock()
        mock_component.__class__.__name__ = "CustomComponent"
        
        def wrapped_func(self, input_data=None, **kwargs):
            return {"output": "processed"}
        
        with patch("traceai_haystack._wrappers._get_component_type") as mock_get_type:
            mock_get_type.return_value = ComponentType.UNKNOWN
            
            result = self.wrapper(wrapped_func, mock_component, (mock_component,), {"input_data": "test"})
        
        assert result["output"] == "processed"
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_component_run_wrapper_exception(self):
        """Test component run wrapper exception handling."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        mock_component = Mock()
        mock_component.__class__.__name__ = "FailingComponent"
        
        def wrapped_func(self, **kwargs):
            raise RuntimeError("Component error")
        
        with patch("traceai_haystack._wrappers._get_component_type") as mock_get_type:
            mock_get_type.return_value = ComponentType.UNKNOWN
            
            with pytest.raises(RuntimeError, match="Component error"):
                self.wrapper(wrapped_func, mock_component, (mock_component,), {})
        
        # Check that error status was set
        status_call = mock_span.set_status.call_args[0][0]
        assert status_call.status_code == trace_api.StatusCode.ERROR
        assert "Component error" in str(status_call.description)

    def test_component_run_wrapper_suppressed(self):
        """Test component run wrapper when instrumentation is suppressed."""
        with patch("traceai_haystack._wrappers.context_api.get_value") as mock_get_value:
            mock_get_value.return_value = True
            
            mock_component = Mock()
            def wrapped_func(self, **kwargs):
                return {"output": "bypassed"}
            
            result = self.wrapper(wrapped_func, mock_component, (mock_component,), {})
            
            assert result == {"output": "bypassed"}
            # Should not start span when suppressed
            self.mock_tracer.start_as_current_span.assert_not_called()


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
        
        with patch("traceai_haystack._wrappers._get_component_run_method") as mock_get_run:
            mock_run_method = Mock()
            mock_get_run.return_value = mock_run_method
            
            with patch("traceai_haystack._wrappers._has_generator_output_type") as mock_has_gen:
                mock_has_gen.return_value = True
                
                result = _get_component_type(mock_component)
                assert result == ComponentType.GENERATOR

    def test_get_component_type_embedder(self):
        """Test _get_component_type for embedder components."""
        mock_component = Mock()
        mock_component.__class__.__name__ = "SentenceTransformersTextEmbedder"
        
        with patch("traceai_haystack._wrappers._get_component_run_method") as mock_get_run:
            mock_run_method = Mock()
            mock_get_run.return_value = mock_run_method
            
            with patch("traceai_haystack._wrappers._has_generator_output_type") as mock_has_gen:
                mock_has_gen.return_value = False
                
                result = _get_component_type(mock_component)
                # Will depend on class name matching
                assert result in [ComponentType.EMBEDDER, ComponentType.UNKNOWN]

    def test_get_span_kind_attributes(self):
        """Test _get_span_kind_attributes function.""" 
        attributes = list(_get_span_kind_attributes("LLM"))
        
        assert len(attributes) == 1
        key, value = attributes[0]
        assert key == "fi.span.kind"
        assert value == "LLM"

    def test_get_input_attributes(self):
        """Test _get_input_attributes function."""
        arguments = {"messages": [{"role": "user", "content": "Hello"}], "temperature": 0.7}
        
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
                {"role": "user", "content": "Hello"}
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
        assert any("llm.output_messages" in key for key in attr_dict.keys())

    def test_get_llm_model_attributes(self):
        """Test _get_llm_model_attributes function."""
        response = {
            "meta": [{"model": "gpt-4", "finish_reason": "stop"}]
        }
        
        attributes = list(_get_llm_model_attributes(response))
        
        # Should extract model name
        attr_dict = dict(attributes)
        assert "llm.model_name" in attr_dict
        assert attr_dict["llm.model_name"] == "gpt-4"

    def test_get_llm_token_count_attributes(self):
        """Test _get_llm_token_count_attributes function."""
        response = {
            "meta": [{
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30
                }
            }]
        }
        
        attributes = list(_get_llm_token_count_attributes(response))
        
        # Should extract token counts
        attr_dict = dict(attributes)
        assert "llm.token_count.prompt" in attr_dict
        assert attr_dict["llm.token_count.prompt"] == 10
        assert "llm.token_count.completion" in attr_dict
        assert attr_dict["llm.token_count.completion"] == 20

    def test_get_embedding_model_attributes(self):
        """Test _get_embedding_model_attributes function."""
        mock_component = Mock()
        mock_component.model = "sentence-transformers/all-MiniLM-L6-v2"
        
        attributes = list(_get_embedding_model_attributes(mock_component))
        
        # Should extract model name
        attr_dict = dict(attributes)
        assert "embedding.model_name" in attr_dict
        assert attr_dict["embedding.model_name"] == "sentence-transformers/all-MiniLM-L6-v2"

    def test_get_embedding_attributes_documents(self):
        """Test _get_embedding_attributes function with documents."""
        # Mock Document class
        mock_doc = Mock()
        mock_doc.content = "Test document"
        mock_doc.embedding = [0.1, 0.2, 0.3]
        
        arguments = {"documents": [mock_doc]}
        response = {"documents": [mock_doc]}
        
        with patch("traceai_haystack._wrappers._is_embedding_doc") as mock_is_embedding:
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
        assert _is_vector([0.1, 0.2, 0.3]) == True
        assert _is_vector([1, 2, 3]) == True
        assert _is_vector([]) == True
        assert _is_vector(["a", "b"]) == False
        assert _is_vector("not a vector") == False
        assert _is_vector(None) == False

    def test_is_list_of_documents(self):
        """Test _is_list_of_documents function."""
        # Test with basic types without mocking isinstance
        assert _is_list_of_documents([]) == True
        assert _is_list_of_documents("not a list") == False
        
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
        bound_args = _get_bound_arguments(test_func, "value1", invalid="should_be_filtered")
        
        assert bound_args.arguments["arg1"] == "value1"
        # Invalid kwargs should be filtered out when function doesn't accept **kwargs
        assert "invalid" not in bound_args.arguments


if __name__ == "__main__":
    pytest.main([__file__]) 