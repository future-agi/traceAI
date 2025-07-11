"""Tests for fi_instrumentation.instrumentation.config module."""

import os
import pytest
from unittest.mock import patch, MagicMock
from opentelemetry.context import get_current, get_value
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY

from fi_instrumentation.instrumentation.config import (
    TraceConfig,
    suppress_tracing,
    REDACTED_VALUE,
    FI_HIDE_LLM_INVOCATION_PARAMETERS,
    FI_HIDE_INPUTS,
    FI_HIDE_OUTPUTS,
    FI_HIDE_INPUT_MESSAGES,
    FI_HIDE_OUTPUT_MESSAGES,
    FI_HIDE_INPUT_IMAGES,
    FI_HIDE_INPUT_TEXT,
    FI_HIDE_OUTPUT_TEXT,
    FI_HIDE_EMBEDDING_VECTORS,
    FI_BASE64_IMAGE_MAX_LENGTH,
    is_base64_url,
)


class TestTraceConfig:
    """Test TraceConfig functionality."""

    def test_trace_config_default_values(self, clean_env):
        """Test TraceConfig with default values."""
        config = TraceConfig()
        
        assert config.hide_llm_invocation_parameters is False
        assert config.hide_inputs is False
        assert config.hide_outputs is False
        assert config.hide_input_messages is False
        assert config.hide_output_messages is False
        assert config.hide_input_images is False
        assert config.hide_input_text is False
        assert config.hide_output_text is False
        assert config.hide_embedding_vectors is False
        assert config.base64_image_max_length == 32_000

    def test_trace_config_explicit_values(self, clean_env):
        """Test TraceConfig with explicitly set values."""
        config = TraceConfig(
            hide_llm_invocation_parameters=True,
            hide_inputs=True,
            hide_outputs=True,
            hide_input_messages=True,
            hide_output_messages=True,
            hide_input_images=True,
            hide_input_text=True,
            hide_output_text=True,
            hide_embedding_vectors=True,
            base64_image_max_length=1000,
        )
        
        assert config.hide_llm_invocation_parameters is True
        assert config.hide_inputs is True
        assert config.hide_outputs is True
        assert config.hide_input_messages is True
        assert config.hide_output_messages is True
        assert config.hide_input_images is True
        assert config.hide_input_text is True
        assert config.hide_output_text is True
        assert config.hide_embedding_vectors is True
        assert config.base64_image_max_length == 1000

    def test_trace_config_env_variables(self, clean_env):
        """Test TraceConfig reading from environment variables."""
        os.environ[FI_HIDE_LLM_INVOCATION_PARAMETERS] = "true"
        os.environ[FI_HIDE_INPUTS] = "true"
        os.environ[FI_HIDE_OUTPUTS] = "true"
        os.environ[FI_HIDE_INPUT_MESSAGES] = "True"
        os.environ[FI_HIDE_OUTPUT_MESSAGES] = "TRUE"
        os.environ[FI_HIDE_INPUT_IMAGES] = "false"
        os.environ[FI_HIDE_INPUT_TEXT] = "false"
        os.environ[FI_HIDE_OUTPUT_TEXT] = "false"
        os.environ[FI_HIDE_EMBEDDING_VECTORS] = "False"
        os.environ[FI_BASE64_IMAGE_MAX_LENGTH] = "5000"
        
        config = TraceConfig()
        
        assert config.hide_llm_invocation_parameters is True
        assert config.hide_inputs is True
        assert config.hide_outputs is True
        assert config.hide_input_messages is True
        assert config.hide_output_messages is True
        assert config.hide_input_images is False
        assert config.hide_input_text is False
        assert config.hide_output_text is False
        assert config.hide_embedding_vectors is False
        assert config.base64_image_max_length == 5000

    def test_trace_config_env_variables_override_explicit(self, clean_env):
        """Test that explicit values override environment variables."""
        os.environ[FI_HIDE_INPUTS] = "true"
        os.environ[FI_BASE64_IMAGE_MAX_LENGTH] = "5000"
        
        config = TraceConfig(
            hide_inputs=False,
            base64_image_max_length=10000,
        )
        
        # Explicit values should override env vars
        assert config.hide_inputs is False
        assert config.base64_image_max_length == 10000

    def test_trace_config_invalid_env_values(self, clean_env):
        """Test TraceConfig with invalid environment variable values."""
        os.environ[FI_HIDE_INPUTS] = "invalid_boolean"
        os.environ[FI_BASE64_IMAGE_MAX_LENGTH] = "not_a_number"
        
        config = TraceConfig()
        
        # Should fall back to defaults for invalid values
        assert config.hide_inputs is False  # default
        assert config.base64_image_max_length == 32_000  # default

    def test_trace_config_frozen(self, clean_env):
        """Test that TraceConfig is frozen (immutable)."""
        config = TraceConfig()
        
        with pytest.raises(Exception):  # FrozenInstanceError in Python 3.7+
            config.hide_inputs = True

    def test_trace_config_mask_callable(self, clean_env):
        """Test masking callable attributes."""
        config = TraceConfig()
        
        # Test with callable that returns a value
        result = config.mask("test_key", lambda: "test_value")
        assert result == "test_value"
        
        # Test with direct value
        result = config.mask("test_key", "direct_value")
        assert result == "direct_value"

    def test_trace_config_mask_with_redaction(self, clean_env):
        """Test masking attributes with potential redaction."""
        config = TraceConfig(hide_inputs=True)
        
        # When hiding is enabled, should return redacted value
        result = config.mask("input.value", "sensitive_input")
        # This should return the redacted value when inputs are hidden
        assert result == "__REDACTED__"


class TestSuppressTracing:
    """Test suppress_tracing context manager."""

    def test_suppress_tracing_sync_context(self):
        """Test suppress_tracing as synchronous context manager."""
        # Initially, suppression should be False or None
        initial_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        assert initial_value is None or initial_value is False
        
        with suppress_tracing():
            # Inside context, suppression should be True
            current_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
            assert current_value is True
        
        # After context, suppression should be back to original
        final_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        assert final_value is None or final_value is False

    @pytest.mark.asyncio
    async def test_suppress_tracing_async_context(self):
        """Test suppress_tracing in async context (using sync version)."""
        # The current implementation doesn't support async context manager properly
        # So we test using the sync version within an async function
        initial_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        assert initial_value is None or initial_value is False
        
        with suppress_tracing():
            # Inside context, suppression should be True
            current_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
            assert current_value is True
        
        # After context, suppression should be back to original
        final_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        assert final_value is None or final_value is False

    def test_suppress_tracing_nested(self):
        """Test nested suppress_tracing contexts."""
        with suppress_tracing():
            assert get_value(_SUPPRESS_INSTRUMENTATION_KEY) is True
            
            with suppress_tracing():
                assert get_value(_SUPPRESS_INSTRUMENTATION_KEY) is True
            
            assert get_value(_SUPPRESS_INSTRUMENTATION_KEY) is True
        
        final_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        assert final_value is None or final_value is False

    def test_suppress_tracing_exception_handling(self):
        """Test suppress_tracing properly handles exceptions."""
        try:
            with suppress_tracing():
                assert get_value(_SUPPRESS_INSTRUMENTATION_KEY) is True
                raise ValueError("Test exception")
        except ValueError:
            pass
        
        # Should still restore original state even after exception
        final_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        assert final_value is None or final_value is False

    @pytest.mark.asyncio
    async def test_suppress_tracing_async_exception_handling(self):
        """Test suppress_tracing properly handles exceptions in async context."""
        try:
            with suppress_tracing():
                assert get_value(_SUPPRESS_INSTRUMENTATION_KEY) is True
                raise ValueError("Test exception")
        except ValueError:
            pass
        
        # Should still restore original state even after exception
        final_value = get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        assert final_value is None or final_value is False


class TestUtilityFunctions:
    """Test utility functions."""

    def test_is_base64_url_valid_base64(self):
        """Test is_base64_url with valid base64 URL."""
        base64_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
        result = is_base64_url(base64_url)
        assert result is True

    def test_is_base64_url_invalid_format(self):
        """Test is_base64_url with invalid format."""
        invalid_url = "https://example.com/image.png"
        result = is_base64_url(invalid_url)
        assert result is False

    def test_is_base64_url_missing_base64_prefix(self):
        """Test is_base64_url with missing base64 prefix."""
        invalid_url = "data:image/png,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
        result = is_base64_url(invalid_url)
        assert result is False

    def test_is_base64_url_empty_string(self):
        """Test is_base64_url with empty string."""
        result = is_base64_url("")
        assert result is False

    def test_is_base64_url_none(self):
        """Test is_base64_url with None."""
        result = is_base64_url(None)
        assert result is False


class TestConstants:
    """Test module constants."""

    def test_redacted_value_constant(self):
        """Test REDACTED_VALUE constant."""
        assert REDACTED_VALUE == "__REDACTED__"

    def test_environment_variable_constants(self):
        """Test environment variable name constants."""
        assert FI_HIDE_LLM_INVOCATION_PARAMETERS == "FI_HIDE_LLM_INVOCATION_PARAMETERS"
        assert FI_HIDE_INPUTS == "FI_HIDE_INPUTS"
        assert FI_HIDE_OUTPUTS == "FI_HIDE_OUTPUTS"
        assert FI_HIDE_INPUT_MESSAGES == "FI_HIDE_INPUT_MESSAGES"
        assert FI_HIDE_OUTPUT_MESSAGES == "FI_HIDE_OUTPUT_MESSAGES"
        assert FI_HIDE_INPUT_IMAGES == "FI_HIDE_INPUT_IMAGES"
        assert FI_HIDE_INPUT_TEXT == "FI_HIDE_INPUT_TEXT"
        assert FI_HIDE_OUTPUT_TEXT == "FI_HIDE_OUTPUT_TEXT"
        assert FI_HIDE_EMBEDDING_VECTORS == "FI_HIDE_EMBEDDING_VECTORS"
        assert FI_BASE64_IMAGE_MAX_LENGTH == "FI_BASE64_IMAGE_MAX_LENGTH" 