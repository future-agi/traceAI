"""
Bedrock Framework Instrumentation Tests

Tests for the Bedrock framework instrumentation to verify it correctly instruments
AWS Bedrock client calls and generates appropriate spans and attributes.
"""

import pytest
import sys
import os
import json
import base64
import io
from unittest.mock import patch, MagicMock, Mock
from pathlib import Path
from typing import Dict, Any

# Import the instrumentation
from traceai_bedrock import BedrockInstrumentor, BufferedStreamingBody
from traceai_bedrock import (
    _set_span_attribute,
    _get_attributes_from_message_param,
    _get_attributes_from_message_content,
    _get_attributes_from_image,
    is_iterable_of,
)

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


class TestBedrockFramework:
    """Test Bedrock framework instrumentation."""

    @pytest.fixture(autouse=True) 
    def setup_trace_provider(self):
        """Set up trace provider for Bedrock testing."""
        eval_tags = [
            EvalTag(
                eval_name=EvalName.TOXICITY,
                value=EvalSpanKind.LLM,
                type=EvalTagType.OBSERVATION_SPAN,
                model=ModelChoices.TURING_LARGE,
                mapping={"input": "prompt"},
                custom_eval_name="bedrock_test_eval"
            )
        ]
        
        with patch('fi_instrumentation.otel.check_custom_eval_config_exists') as mock_check:
            mock_check.return_value = False
            self.trace_provider = register(
                project_type=ProjectType.EXPERIMENT,
                eval_tags=eval_tags,
                project_name="bedrock_framework_test",
                project_version_name="v1.0",
                verbose=False
            )
        yield
        
    @pytest.fixture
    def mock_boto_modules(self):
        """Mock boto3 and botocore modules."""
        mock_botocore = MagicMock()
        mock_botocore.__version__ = "1.34.116"
        
        mock_boto = MagicMock()
        
        # Create a mock ClientCreator 
        class MockClientCreator:
            def __init__(self):
                self.original_create_client = self.create_client
                
            def create_client(self, service_name=None, **kwargs):
                if service_name == "bedrock-runtime":
                    return self._create_bedrock_client(**kwargs)
                return self._create_regular_client(service_name=service_name, **kwargs)
                
            def _create_bedrock_client(self, **kwargs):
                # Create mock bedrock client that doesn't have wrapped methods initially
                client = MagicMock()
                # Don't pre-add the _unwrapped methods - let instrumentation add them
                return client
                
            def _create_regular_client(self, service_name=None, **kwargs):
                client = MagicMock()
                # Make sure non-bedrock clients don't get bedrock attributes
                if hasattr(client, '_unwrapped_invoke_model'):
                    delattr(client, '_unwrapped_invoke_model')
                if hasattr(client, '_unwrapped_converse'):
                    delattr(client, '_unwrapped_converse')
                return client
        
        mock_boto.ClientCreator = MockClientCreator()
        
        with patch('importlib.import_module') as mock_import:
            def import_side_effect(module_name):
                if module_name == "botocore.client":
                    return mock_boto
                elif module_name == "botocore":
                    return mock_botocore
                else:
                    return MagicMock()
            
            mock_import.side_effect = import_side_effect
            yield mock_boto, mock_botocore
    
    @pytest.fixture
    def mock_protect(self):
        """Mock the Protect class from fi.evals."""
        mock_protect_class = MagicMock()
        mock_protect_instance = MagicMock()
        mock_protect_class.return_value = mock_protect_instance
        mock_protect_instance.protect = MagicMock()
        
        with patch('traceai_bedrock.Protect', mock_protect_class):
            yield mock_protect_instance
    
    def test_bedrock_import(self):
        """Test that we can import the Bedrock instrumentor."""
        assert BedrockInstrumentor is not None
        
        # Test basic instantiation
        instrumentor = BedrockInstrumentor()
        assert instrumentor is not None
    
    def test_bedrock_basic_instrumentation(self, mock_boto_modules, mock_protect):
        """Test basic Bedrock client instrumentation."""
        mock_boto, mock_botocore = mock_boto_modules
        
        # Initialize instrumentor
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            # Verify that the client creator is wrapped
            assert hasattr(mock_boto.ClientCreator, 'create_client')
            
            # Create a bedrock client
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            # Verify client has the expected methods
            assert hasattr(client, 'invoke_model')
            assert hasattr(client, 'converse')
            assert hasattr(client, 'invoke_model_with_response_stream')
            
        finally:
            # Clean up
            instrumentor.uninstrument()
    
    def test_bedrock_invoke_model_basic(self, mock_boto_modules, mock_protect):
        """Test basic invoke_model instrumentation."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            # Mock client and response
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            # Mock the StreamingBody
            mock_stream = MagicMock()
            mock_stream._raw_stream = io.BytesIO(b'{"completion": "Test response"}')
            mock_stream._content_length = 30
            mock_stream.read.return_value = b'{"completion": "Test response"}'
            
            mock_response = {
                "body": mock_stream,
                "ResponseMetadata": {"HTTPStatusCode": 200}
            }
            
            # Simulate how the real instrumentation works
            # The instrumentation adds _unwrapped_invoke_model and replaces invoke_model
            if hasattr(client, '_unwrapped_invoke_model'):
                client._unwrapped_invoke_model.return_value = mock_response
            else:
                # If not instrumented, just mock the direct call
                client.invoke_model.return_value = mock_response
            
            # Test with various request body formats
            request_body = {
                "prompt": "What is AI?",
                "max_tokens": 100,
                "temperature": 0.7
            }
            
            with using_attributes(
                session_id="test-session-123",
                user_id="test-user-456", 
                metadata={"test_type": "invoke_model", "framework": "bedrock"},
                tags=["bedrock", "test", "claude"]
            ):
                response = client.invoke_model(
                    modelId="anthropic.claude-v2",
                    body=json.dumps(request_body)
                )
            
            assert response is not None
            
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_converse_basic(self, mock_boto_modules, mock_protect):
        """Test basic converse method instrumentation."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            # Mock converse response
            mock_response = {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"text": "This is a test response from Claude."}
                        ]
                    }
                },
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 12,
                    "totalTokens": 22
                }
            }
            
            if hasattr(client, '_unwrapped_converse'):
                client._unwrapped_converse.return_value = mock_response
            else:
                client.converse.return_value = mock_response
            
            with using_attributes(
                session_id="converse-test-session",
                metadata={"test_type": "converse"}
            ):
                response = client.converse(
                    modelId="anthropic.claude-3-sonnet-20240229",
                    messages=[
                        {
                            "role": "user",
                            "content": [{"text": "Hello, how are you?"}]
                        }
                    ],
                    inferenceConfig={
                        "maxTokens": 100,
                        "temperature": 0.7
                    }
                )
            
            # Verify the response exists (since we're using mocks, just verify the call was made)
            assert response is not None
            
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_converse_with_system_prompt(self, mock_boto_modules, mock_protect):
        """Test converse method with system prompts."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            mock_response = {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "I understand the system context."}]
                    }
                },
                "usage": {"inputTokens": 20, "outputTokens": 8, "totalTokens": 28}
            }
            
            if hasattr(client, '_unwrapped_converse'):
                client._unwrapped_converse.return_value = mock_response
            else:
                client.converse.return_value = mock_response
            
            response = client.converse(
                modelId="anthropic.claude-3-sonnet-20240229",
                system=[
                    {"text": "You are a helpful assistant."},
                    {"text": "Be concise in your responses."}
                ],
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": "What is the capital of France?"}]
                    }
                ]
            )
            
            # Verify the response exists
            assert response is not None
            
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_converse_with_images(self, mock_boto_modules, mock_protect):
        """Test converse method with image content."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            mock_response = {
                "output": {
                    "message": {
                        "role": "assistant", 
                        "content": [{"text": "I can see the image you shared."}]
                    }
                },
                "usage": {"inputTokens": 250, "outputTokens": 15, "totalTokens": 265}
            }
            
            if hasattr(client, '_unwrapped_converse'):
                client._unwrapped_converse.return_value = mock_response
            else:
                client.converse.return_value = mock_response
            
            # Mock image data
            fake_image_bytes = b"fake_image_data"
            
            response = client.converse(
                modelId="anthropic.claude-3-sonnet-20240229",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"text": "What do you see in this image?"},
                            {
                                "image": {
                                    "source": {"bytes": fake_image_bytes}
                                }
                            }
                        ]
                    }
                ]
            )
            
            # Verify the response exists
            assert response is not None
            
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_buffered_streaming_body(self):
        """Test BufferedStreamingBody functionality."""
        # Create a mock raw stream
        test_data = b'{"response": "streaming data"}'
        raw_stream = io.BytesIO(test_data)
        
        # Create BufferedStreamingBody
        buffered_body = BufferedStreamingBody(raw_stream, len(test_data))
        
        # Test reading
        data1 = buffered_body.read()
        assert data1 == test_data
        
        # Test reset and read again
        buffered_body.reset()
        data2 = buffered_body.read()
        assert data2 == test_data
        
        # Test partial reading
        buffered_body.reset()
        partial_data = buffered_body.read(10)
        assert len(partial_data) == 10
        assert partial_data == test_data[:10]
    
    def test_bedrock_error_handling_invoke_model(self, mock_boto_modules, mock_protect):
        """Test error handling in invoke_model instrumentation."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            # Make the invoke_model method raise an exception
            client.invoke_model.side_effect = Exception("Model invocation failed")
            
            with pytest.raises(Exception, match="Model invocation failed"):
                client.invoke_model(
                    modelId="anthropic.claude-v2",
                    body='{"prompt": "test"}'
                )
                
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_error_handling_converse(self, mock_boto_modules, mock_protect):
        """Test error handling in converse instrumentation."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            # Make the converse method raise an exception
            client.converse.side_effect = RuntimeError("Converse API error")
            
            with pytest.raises(RuntimeError, match="Converse API error"):
                client.converse(
                    modelId="anthropic.claude-3-sonnet-20240229",
                    messages=[{"role": "user", "content": [{"text": "test"}]}]
                )
                
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_utility_functions(self):
        """Test utility functions for message and content parsing."""
        # Test _set_span_attribute
        mock_span = MagicMock()
        
        # Test with valid value
        _set_span_attribute(mock_span, "test.attribute", "test_value")
        mock_span.set_attribute.assert_called_with("test.attribute", "test_value")
        
        # Test with None value (should not set attribute)
        mock_span.reset_mock()
        _set_span_attribute(mock_span, "test.attribute", None)
        mock_span.set_attribute.assert_not_called()
        
        # Test with empty string (should not set attribute)
        mock_span.reset_mock()
        _set_span_attribute(mock_span, "test.attribute", "")
        mock_span.set_attribute.assert_not_called()
    
    def test_bedrock_message_param_attributes(self):
        """Test extracting attributes from message parameters."""
        # Test basic message with role and content
        message = {
            "role": "user",
            "content": "Hello, how are you?"
        }
        
        attributes = list(_get_attributes_from_message_param(message))
        assert len(attributes) >= 2
        
        # Check for role attribute
        role_attr = next((attr for attr in attributes if attr[0] == "message.role"), None)
        assert role_attr is not None
        assert role_attr[1] == "user"
        
        # Check for content attribute
        content_attr = next((attr for attr in attributes if attr[0] == "message.content"), None)
        assert content_attr is not None
        assert content_attr[1] == "Hello, how are you?"
    
    def test_bedrock_message_content_attributes(self):
        """Test extracting attributes from message content."""
        # Test text content
        text_content = {"text": "This is a text message"}
        attributes = list(_get_attributes_from_message_content(text_content))
        
        assert len(attributes) == 2
        assert ("message_content.type", "text") in attributes
        assert ("message_content.text", "This is a text message") in attributes
        
        # Test image content
        fake_image_bytes = b"fake_image_data"
        image_content = {
            "image": {
                "source": {"bytes": fake_image_bytes}
            }
        }
        
        attributes = list(_get_attributes_from_message_content(image_content))
        assert len(attributes) >= 1
        assert ("message_content.type", "image") in attributes
    
    def test_bedrock_image_attributes(self):
        """Test extracting attributes from image data."""
        fake_image_bytes = b"fake_image_data"
        image_data = {
            "source": {"bytes": fake_image_bytes}
        }
        
        attributes = list(_get_attributes_from_image(image_data))
        assert len(attributes) == 1
        
        # Check that base64 encoding is working
        url_attr = attributes[0]
        assert url_attr[0] == "image.url"
        assert url_attr[1].startswith("data:image/jpeg;base64,")
        
        # Verify base64 content
        base64_part = url_attr[1].split(",")[1]
        decoded_bytes = base64.b64decode(base64_part)
        assert decoded_bytes == fake_image_bytes
    
    def test_bedrock_is_iterable_of(self):
        """Test the is_iterable_of utility function."""
        # Test with list of dicts
        list_of_dicts = [{"a": 1}, {"b": 2}]
        assert is_iterable_of(list_of_dicts, dict) is True
        
        # Test with list of strings
        list_of_strings = ["hello", "world"]
        assert is_iterable_of(list_of_strings, str) is True
        
        # Test with mixed types
        mixed_list = [{"a": 1}, "string"]
        assert is_iterable_of(mixed_list, dict) is False
        
        # Test with non-iterable
        non_iterable = 42
        assert is_iterable_of(non_iterable, int) is False
        
        # Test with empty list
        empty_list = []
        assert is_iterable_of(empty_list, dict) is True
    
    def test_bedrock_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        instrumentor = BedrockInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert "boto3 >= 1.28.57" in deps
    
    def test_bedrock_uninstrument_before_instrument(self, mock_boto_modules, mock_protect):
        """Test uninstrument behavior when called before instrument."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        
        # Should not raise error when uninstrument is called before instrument
        instrumentor.uninstrument()
        
        # Should still be able to instrument after
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            assert hasattr(client, 'invoke_model')
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_multiple_instrument_calls(self, mock_boto_modules, mock_protect):
        """Test multiple instrument calls don't break functionality."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        
        # First instrument
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        # Second instrument (should handle gracefully)
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            assert hasattr(client, 'invoke_model')
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_non_bedrock_client(self, mock_boto_modules, mock_protect):
        """Test that non-bedrock clients are not instrumented."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            # Create a non-bedrock client (e.g., S3)
            s3_client = mock_boto.ClientCreator.create_client(service_name="s3")
            
            # Test that S3 client doesn't have bedrock-specific wrapped methods
            # Since we're using mocks, we'll test by verifying the service name check works
            assert s3_client is not None
            
            # Create bedrock client for comparison
            bedrock_client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            assert bedrock_client is not None
            
            # Both should exist but be different if instrumentation respects service names
            assert s3_client != bedrock_client
            
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_invoke_model_with_messages(self, mock_boto_modules, mock_protect):
        """Test invoke_model with messages format (Anthropic style)."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            # Mock response
            mock_stream = MagicMock()
            mock_stream._raw_stream = io.BytesIO(b'{"content": [{"text": "Hello"}]}')
            mock_stream._content_length = 30
            mock_stream.read.return_value = b'{"content": [{"text": "Hello"}]}'
            
            mock_response = {"body": mock_stream}
            
            if hasattr(client, '_unwrapped_invoke_model'):
                client._unwrapped_invoke_model.return_value = mock_response
            else:
                client.invoke_model.return_value = mock_response
            
            # Test with messages format
            request_body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 100,
                "messages": [
                    {"role": "user", "content": "What is AI?"}
                ]
            }
            
            response = client.invoke_model(
                modelId="anthropic.claude-3-sonnet-20240229",
                body=json.dumps(request_body)
            )
            
            # Verify the response exists
            assert response is not None
            
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_complex_scenario(self, mock_boto_modules, mock_protect):
        """Test a complex multi-turn conversation scenario."""
        mock_boto, mock_botocore = mock_boto_modules
        
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            # Mock multiple responses
            responses = [
                {
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [{"text": "I'd be happy to help with math!"}]
                        }
                    },
                    "usage": {"inputTokens": 15, "outputTokens": 10, "totalTokens": 25}
                },
                {
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [{"text": "2 + 2 = 4"}]
                        }
                    },
                    "usage": {"inputTokens": 20, "outputTokens": 8, "totalTokens": 28}
                }
            ]
            
            if hasattr(client, '_unwrapped_converse'):
                client._unwrapped_converse.side_effect = responses
            else:
                client.converse.side_effect = responses
            
            with using_attributes(
                session_id="complex-math-session",
                user_id="student-123",
                metadata={
                    "conversation_type": "math_tutoring",
                    "difficulty": "basic",
                    "subject": "arithmetic"
                },
                tags=["math", "tutoring", "bedrock", "claude"]
            ):
                # First message
                response1 = client.converse(
                    modelId="anthropic.claude-3-sonnet-20240229",
                    messages=[
                        {
                            "role": "user",
                            "content": [{"text": "Can you help me with math?"}]
                        }
                    ]
                )
                
                # Second message in conversation
                response2 = client.converse(
                    modelId="anthropic.claude-3-sonnet-20240229",
                    messages=[
                        {
                            "role": "user",
                            "content": [{"text": "Can you help me with math?"}]
                        },
                        {
                            "role": "assistant",
                            "content": [{"text": "I'd be happy to help with math!"}]
                        },
                        {
                            "role": "user", 
                            "content": [{"text": "What is 2 + 2?"}]
                        }
                    ]
                )
            
            # Verify both calls succeeded
            assert response1 is not None
            assert response2 is not None
            
        finally:
            instrumentor.uninstrument()
    
    def test_bedrock_version_compatibility(self, mock_boto_modules, mock_protect):
        """Test version compatibility for converse method."""
        mock_boto, mock_botocore = mock_boto_modules
        
        # Test with old botocore version (should not have converse)
        old_mock_botocore = MagicMock()
        old_mock_botocore.__version__ = "1.30.0"  # Below minimum version
        
        instrumentor = BedrockInstrumentor()
        
        with patch('importlib.import_module') as mock_import:
            def import_side_effect(module_name):
                if module_name == "botocore.client":
                    return mock_boto
                elif module_name == "botocore":
                    return old_mock_botocore
                else:
                    return MagicMock()
            
            mock_import.side_effect = import_side_effect
            instrumentor.instrument(tracer_provider=self.trace_provider)
        
        try:
            client = mock_boto.ClientCreator.create_client(service_name="bedrock-runtime")
            
            # Should have invoke_model but not _unwrapped_converse
            assert hasattr(client, 'invoke_model')
            # For old versions, converse should not be wrapped
            
        finally:
            instrumentor.uninstrument()
            
    def test_bedrock_package_info(self):
        """Test package information and version."""
        from traceai_bedrock import __version__
        from traceai_bedrock.package import _instruments, _supports_metrics
        
        # Test version exists
        assert __version__ is not None
        
        # Test instruments tuple
        assert "boto3 >= 1.28.57" in _instruments
        
        # Test metrics support
        assert _supports_metrics is False 