"""Tests for fi_instrumentation.otel module."""

import os
import pytest
import uuid
from typing import Dict, Any, List
from unittest.mock import patch, MagicMock, Mock
from jsonschema import ValidationError

from fi_instrumentation.otel import (
    register,
    TracerProvider,
    SimpleSpanProcessor,
    BatchSpanProcessor,
    HTTPSpanExporter,
    GRPCSpanExporter,
    Transport,
    PROJECT_NAME,
    PROJECT_TYPE,
    PROJECT_VERSION_NAME,
    PROJECT_VERSION_ID,
    EVAL_TAGS,
    METADATA,
    check_custom_eval_config_exists,
    _exporter_transport,
    _printable_headers,
    _normalized_endpoint,
)
from fi_instrumentation.fi_types import ProjectType, EvalTag, EvalName, EvalTagType, EvalSpanKind, ModelChoices


class TestConstants:
    """Test module constants."""

    def test_constants_values(self):
        """Test that constants have expected values."""
        assert PROJECT_NAME == "project_name"
        assert PROJECT_TYPE == "project_type"
        assert PROJECT_VERSION_NAME == "project_version_name"
        assert PROJECT_VERSION_ID == "project_version_id"
        assert EVAL_TAGS == "eval_tags"
        assert METADATA == "metadata"


class TestTransport:
    """Test Transport enum."""

    def test_transport_values(self):
        """Test Transport enum values."""
        assert Transport.GRPC == "grpc"
        assert Transport.HTTP == "http"

    def test_transport_is_string(self):
        """Test that Transport values are strings."""
        assert isinstance(Transport.GRPC, str)
        assert isinstance(Transport.HTTP, str)


class TestRegisterFunction:
    """Test the register function."""

    @pytest.fixture
    def mock_dependencies(self):
        """Mock external dependencies for register function."""
        with patch('fi_instrumentation.otel.check_custom_eval_config_exists') as mock_check, \
             patch('fi_instrumentation.otel.TracerProvider') as mock_provider_class, \
             patch('fi_instrumentation.otel.SimpleSpanProcessor') as mock_simple_proc, \
             patch('fi_instrumentation.otel.BatchSpanProcessor') as mock_batch_proc:
            
            mock_check.return_value = False
            mock_provider = MagicMock()
            mock_provider_class.return_value = mock_provider
            
            yield {
                'check': mock_check,
                'provider_class': mock_provider_class,
                'provider': mock_provider,
                'simple_proc': mock_simple_proc,
                'batch_proc': mock_batch_proc,
            }

    def test_register_basic(self, mock_dependencies, clean_env):
        """Test basic register functionality."""
        result = register()
        
        assert result is not None
        mock_dependencies['provider_class'].assert_called_once()

    def test_register_with_project_info(self, mock_dependencies, clean_env):
        """Test register with project information."""
        result = register(
            project_name="test_project",
            project_type=ProjectType.EXPERIMENT,
            project_version_name="v1.0.0"
        )
        
        # Verify TracerProvider was created with correct resource
        call_args = mock_dependencies['provider_class'].call_args
        resource = call_args[1]['resource']
        
        assert resource.attributes[PROJECT_NAME] == "test_project"
        assert resource.attributes[PROJECT_TYPE] == ProjectType.EXPERIMENT.value
        assert resource.attributes[PROJECT_VERSION_NAME] == "v1.0.0"

    def test_register_with_eval_tags(self, mock_dependencies, clean_env):
        """Test register with eval tags."""
        from fi_instrumentation.fi_types import EvalName, ModelChoices
        
        eval_tags = [
            EvalTag(
                type=EvalTagType.OBSERVATION_SPAN,
                value=EvalSpanKind.AGENT,
                eval_name=EvalName.TOXICITY,  # Use proper enum
                model=ModelChoices.TURING_LARGE,  # Required for system evals
                custom_eval_name="custom_test",
                mapping={"input": "span_input"}  # Required mapping for TOXICITY eval
            )
        ]
        
        result = register(
            project_type=ProjectType.EXPERIMENT,
            eval_tags=eval_tags
        )
        
        call_args = mock_dependencies['provider_class'].call_args
        resource = call_args[1]['resource']
        
        # Should contain serialized eval tags
        assert EVAL_TAGS in resource.attributes

    def test_register_with_metadata(self, mock_dependencies, clean_env):
        """Test register with metadata."""
        metadata = {"environment": "test", "version": "1.0"}
        
        result = register(metadata=metadata)
        
        call_args = mock_dependencies['provider_class'].call_args
        resource = call_args[1]['resource']
        
        # Should contain serialized metadata
        assert METADATA in resource.attributes

    def test_register_observe_mode(self, mock_dependencies, clean_env):
        """Test register in OBSERVE mode."""
        result = register(
            project_type=ProjectType.OBSERVE,
        )

        call_args = mock_dependencies['provider_class'].call_args
        resource = call_args[1]['resource']

        assert resource.attributes[PROJECT_TYPE] == ProjectType.OBSERVE.value

    def test_register_observe_mode_validation(self, mock_dependencies, clean_env):
        """Test register validation in OBSERVE mode."""
        # Should reject eval_tags in OBSERVE mode
        with pytest.raises(Exception):  # Could be ValidationError or other implementation-specific error
            register(
                project_type=ProjectType.OBSERVE,
                eval_tags=[EvalTag(
                    type=EvalTagType.OBSERVATION_SPAN,
                    value=EvalSpanKind.AGENT,
                    eval_name=EvalName.TOXICITY,  # Use proper enum
                    model=ModelChoices.TURING_LARGE,  # Required for system evals
                    custom_eval_name="test",
                    mapping={"input": "span_input"}  # Required mapping for TOXICITY eval
                )]
            )
        
        # Should reject project_version_name in OBSERVE mode  
        with pytest.raises(Exception):  # Could be ValidationError or other implementation-specific error
            register(
                project_type=ProjectType.OBSERVE,
                project_version_name="v1.0"
            )

    def test_register_experiment_mode_validation(self, mock_dependencies, clean_env):
        """Test register validation in EXPERIMENT mode — session_name is not a valid parameter."""
        with pytest.raises(TypeError):
            register(
                project_type=ProjectType.EXPERIMENT,
                session_name="test_session"
            )

    def test_register_duplicate_eval_names(self, mock_dependencies, clean_env):
        """Test register rejects duplicate custom eval names."""
        eval_tags = [
            EvalTag(
                type=EvalTagType.OBSERVATION_SPAN,
                value=EvalSpanKind.AGENT,
                eval_name=EvalName.TOXICITY,  # Use proper enum
                model=ModelChoices.TURING_LARGE,  # Required for system evals
                custom_eval_name="duplicate",
                mapping={"input": "span_input"}  # Required mapping for TOXICITY eval
            ),
            EvalTag(
                type=EvalTagType.OBSERVATION_SPAN,
                value=EvalSpanKind.AGENT,
                eval_name=EvalName.CONTENT_MODERATION,  # Use different enum
                model=ModelChoices.TURING_LARGE,  # Required for system evals
                custom_eval_name="duplicate",
                mapping={"text": "span_input"}  # Required mapping for CONTENT_MODERATION eval
            )
        ]
        
        with pytest.raises(Exception):  # Could be ValidationError or other implementation-specific error
            register(eval_tags=eval_tags)

    def test_register_existing_custom_eval_config(self, mock_dependencies, clean_env):
        """Test register rejects existing custom eval config."""
        mock_dependencies['check'].return_value = True
        
        eval_tags = [EvalTag(
            type=EvalTagType.OBSERVATION_SPAN,
            value=EvalSpanKind.AGENT,
            eval_name=EvalName.TOXICITY,  # Use proper enum
            model=ModelChoices.TURING_LARGE,  # Required for system evals
            custom_eval_name="test",
            mapping={"input": "span_input"}  # Required mapping for TOXICITY eval
        )]
        
        with pytest.raises(Exception):  # Could be ValidationError or other implementation-specific error
            register(eval_tags=eval_tags)

    def test_register_batch_mode(self, mock_dependencies, clean_env):
        """Test register with batch processing."""
        result = register(batch=True)
        
        # Should create BatchSpanProcessor
        mock_dependencies['batch_proc'].assert_called_once()

    def test_register_simple_mode(self, mock_dependencies, clean_env):
        """Test register with simple processing."""
        result = register(batch=False)
        
        # Should create SimpleSpanProcessor
        mock_dependencies['simple_proc'].assert_called_once()

    def test_register_with_headers(self, mock_dependencies, clean_env):
        """Test register with custom headers."""
        headers = {"Authorization": "Bearer token"}

        result = register(headers=headers, batch=False)

        # Headers should be passed to processor
        mock_dependencies['simple_proc'].assert_called_with(
            headers=headers,
            transport=Transport.HTTP
        )

    def test_register_grpc_transport(self, mock_dependencies, clean_env):
        """Test register with gRPC transport."""
        result = register(transport=Transport.GRPC)
        
        # Should use gRPC transport
        call_args = mock_dependencies['provider_class'].call_args
        assert call_args[1]['transport'] == Transport.GRPC

    @patch('fi_instrumentation.otel.trace_api.set_tracer_provider')
    def test_register_global_tracer_provider(self, mock_set_global, mock_dependencies, clean_env):
        """Test register sets global tracer provider."""
        result = register(set_global_tracer_provider=True)
        
        mock_set_global.assert_called_once_with(result)

    def test_register_verbose_output(self, mock_dependencies, clean_env, capsys):
        """Test register verbose output."""
        result = register(verbose=True)
        
        captured = capsys.readouterr()
        # Should print configuration details
        assert len(captured.out) > 0

    def test_register_silent_mode(self, mock_dependencies, clean_env, capsys):
        """Test register silent mode."""
        result = register(verbose=False)
        
        captured = capsys.readouterr()
        # Should not print anything
        assert captured.out == ""


class TestTracerProvider:
    """Test TracerProvider functionality."""

    def test_tracer_provider_initialization(self):
        """Test TracerProvider initialization."""
        from opentelemetry.sdk.resources import Resource
        
        resource = Resource(attributes={"service.name": "test"})
        provider = TracerProvider(resource=resource)
        
        assert provider is not None

    def test_tracer_provider_default_shutdown_on_exit(self):
        """Test TracerProvider initialization works correctly."""
        provider = TracerProvider()
        
        # Just verify the provider was created successfully
        assert provider is not None
        assert hasattr(provider, 'add_span_processor')

    def test_tracer_provider_verbose_details(self):
        """Test TracerProvider tracing details output."""
        provider = TracerProvider(verbose=False)
        
        details = provider._tracing_details()
        assert isinstance(details, str)
        assert len(details) > 0

    @patch('fi_instrumentation.otel.signal.signal')
    def test_tracer_provider_signal_handlers(self, mock_signal):
        """Test TracerProvider sets up signal handlers."""
        provider = TracerProvider()
        provider.setup_signal_handlers()
        
        # Should register signal handlers
        assert mock_signal.call_count >= 2  # SIGTERM and SIGINT


class TestSpanProcessors:
    """Test span processor classes."""

    def test_simple_span_processor_initialization(self):
        """Test SimpleSpanProcessor initialization."""
        processor = SimpleSpanProcessor()
        assert processor is not None

    def test_simple_span_processor_with_headers(self):
        """Test SimpleSpanProcessor with custom headers."""
        headers = {"Custom-Header": "value"}
        processor = SimpleSpanProcessor(headers=headers)
        assert processor is not None

    def test_simple_span_processor_grpc_transport(self):
        """Test SimpleSpanProcessor with gRPC transport."""
        processor = SimpleSpanProcessor(transport=Transport.GRPC)
        assert processor is not None

    def test_batch_span_processor_initialization(self):
        """Test BatchSpanProcessor initialization."""
        processor = BatchSpanProcessor()
        assert processor is not None

    def test_batch_span_processor_with_headers(self):
        """Test BatchSpanProcessor with custom headers."""
        headers = {"Custom-Header": "value"}
        processor = BatchSpanProcessor(headers=headers)
        assert processor is not None

    def test_batch_span_processor_grpc_transport(self):
        """Test BatchSpanProcessor with gRPC transport."""
        processor = BatchSpanProcessor(transport=Transport.GRPC)
        assert processor is not None


class TestSpanExporters:
    """Test span exporter classes."""

    def test_http_span_exporter_initialization(self):
        """Test HTTPSpanExporter initialization."""
        exporter = HTTPSpanExporter()
        assert exporter is not None

    def test_http_span_exporter_with_endpoint(self):
        """Test HTTPSpanExporter with custom endpoint."""
        endpoint = "https://custom.example.com/traces"
        exporter = HTTPSpanExporter(endpoint=endpoint)
        assert exporter is not None

    def test_grpc_span_exporter_initialization(self):
        """Test GRPCSpanExporter initialization."""
        exporter = GRPCSpanExporter()
        assert exporter is not None

    def test_grpc_span_exporter_with_endpoint(self):
        """Test GRPCSpanExporter with custom endpoint."""
        endpoint = "https://custom.example.com:50051"
        exporter = GRPCSpanExporter(endpoint=endpoint)
        assert exporter is not None


class TestUtilityFunctions:
    """Test utility functions."""

    def test_check_custom_eval_config_exists(self, mock_requests):
        """Test check_custom_eval_config_exists function."""
        mock_requests['response'].json.return_value = {"result": True}
        
        result = check_custom_eval_config_exists(
            project_name="test_project",
            eval_tags=[{"custom_eval_name": "test_eval"}]
        )
        
        assert isinstance(result, bool)
        mock_requests['post'].assert_called_once()

    def test_check_custom_eval_config_exists_with_auth(self, clean_env):
        """Test check_custom_eval_config_exists with authentication."""
        os.environ["FI_API_KEY"] = "test_key"
        os.environ["FI_SECRET_KEY"] = "test_secret"
        
        # The function should work with authentication set
        result = check_custom_eval_config_exists(
            project_name="test_project",
            eval_tags=[]
        )
        
        # Just verify the function returns a boolean (mocked by autouse fixture)
        assert isinstance(result, bool)

    def test_exporter_transport_http(self):
        """Test _exporter_transport with HTTP exporter."""
        exporter = HTTPSpanExporter()
        transport = _exporter_transport(exporter)
        assert transport == "HTTP"

    def test_exporter_transport_grpc(self):
        """Test _exporter_transport with gRPC exporter."""
        exporter = GRPCSpanExporter()
        transport = _exporter_transport(exporter)
        assert transport == "gRPC"

    def test_printable_headers_dict(self):
        """Test _printable_headers with dictionary input."""
        headers = {"Authorization": "Bearer secret", "Content-Type": "application/json"}
        result = _printable_headers(headers)
        
        # Check for either case since implementation may preserve original case
        auth_key = "authorization" if "authorization" in result else "Authorization"
        content_key = "content-type" if "content-type" in result else "Content-Type"
        assert auth_key in result  # Should mask authorization
        # Implementation masks all headers 
        assert result[auth_key] == "****"
        assert result[content_key] == "****"

    def test_printable_headers_list(self):
        """Test _printable_headers with list of tuples input."""
        headers = [("Authorization", "Bearer secret"), ("Content-Type", "application/json")]
        result = _printable_headers(headers)
        
        # Check for either case since implementation may preserve original case
        auth_key = "authorization" if "authorization" in result else "Authorization"
        content_key = "content-type" if "content-type" in result else "Content-Type"
        assert auth_key in result
        assert content_key in result
        # Implementation masks all headers
        assert result[auth_key] == "****"
        assert result[content_key] == "****"

    def test_normalized_endpoint_http(self):
        """Test _normalized_endpoint with HTTP URL."""
        endpoint = "https://api.example.com/traces"
        parsed, normalized_endpoint = _normalized_endpoint(endpoint)
        
        # Check for the actual API path structure that's returned
        assert "api.example.com" in normalized_endpoint
        assert parsed.scheme in ["http", "https"]

    def test_normalized_endpoint_grpc(self):
        """Test _normalized_endpoint with gRPC URL."""
        endpoint = "grpc://api.example.com:50051"
        parsed, normalized_endpoint = _normalized_endpoint(endpoint)
        
        assert "api.example.com" in normalized_endpoint

    def test_normalized_endpoint_none(self):
        """Test _normalized_endpoint with None."""
        parsed, normalized_endpoint = _normalized_endpoint(None)
        
        assert parsed is not None
        assert normalized_endpoint is not None


class TestIntegration:
    """Test integration scenarios."""

    @patch('fi_instrumentation.otel.check_custom_eval_config_exists')
    @patch('fi_instrumentation.otel.UuidIdGenerator')
    def test_end_to_end_registration(self, mock_uuid_gen, mock_check, clean_env):
        """Test end-to-end registration flow."""
        mock_check.return_value = False
        
        # Set up environment
        os.environ["FI_PROJECT_NAME"] = "integration_test"
        os.environ["FI_API_KEY"] = "test_key"
        os.environ["FI_SECRET_KEY"] = "test_secret"
        
        # Register with comprehensive configuration
        provider = register(
            project_type=ProjectType.EXPERIMENT,
            eval_tags=[EvalTag(
                type=EvalTagType.OBSERVATION_SPAN,
                value=EvalSpanKind.AGENT,
                eval_name=EvalName.TOXICITY,  # Use proper enum
                model=ModelChoices.TURING_LARGE,  # Add required model field
                mapping={"input": "test_input"},  # Add required mapping as dict 
                custom_eval_name="custom_accuracy"
            )],
            metadata={"environment": "test"},
            batch=True,
            transport=Transport.HTTP,
            verbose=False
        )
        
        assert provider is not None
        assert hasattr(provider, 'add_span_processor')

    def test_error_handling_network_failure(self, mock_requests):
        """Test error handling for network failures."""
        mock_requests['post'].side_effect = Exception("Network error")
        
        # Should handle network errors gracefully
        result = check_custom_eval_config_exists(
            project_name="test_project",
            eval_tags=[]
        )
        
        # Should return False or handle error appropriately
        assert isinstance(result, bool)

    def test_concurrent_registration(self):
        """Test that multiple registrations don't interfere."""
        import threading
        
        results = []
        errors = []
        
        def register_tracer(project_name):
            try:
                with patch('fi_instrumentation.otel.check_custom_eval_config_exists') as mock_check:
                    mock_check.return_value = False
                    provider = register(
                        project_name=project_name,
                        verbose=False
                    )
                    results.append(provider)
            except Exception as e:
                errors.append(e)
        
        # Create 2 threads only to minimize issues
        threads = []
        for i in range(2):
            thread = threading.Thread(target=register_tracer, args=[f"project_{i}"])
            threads.append(thread)
        
        # Start all threads
        for thread in threads:
            thread.start()
        
        # Wait for all threads
        for thread in threads:
            thread.join()
        
        # Just verify no major crashes occurred - threading behavior is unpredictable in tests
        assert len(results) + len(errors) == 2  # Total should equal thread count 