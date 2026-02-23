"""Tests for Pydantic AI semantic attributes."""

import pytest


class TestPydanticAISpanKind:
    """Test PydanticAISpanKind enum."""

    def test_agent_run_value(self):
        """Test agent run span kind value."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        assert PydanticAISpanKind.AGENT_RUN.value == "agent_run"

    def test_model_request_value(self):
        """Test model request span kind value."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        assert PydanticAISpanKind.MODEL_REQUEST.value == "model_request"

    def test_tool_call_value(self):
        """Test tool call span kind value."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        assert PydanticAISpanKind.TOOL_CALL.value == "tool_call"

    def test_result_validation_value(self):
        """Test result validation span kind value."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        assert PydanticAISpanKind.RESULT_VALIDATION.value == "result_validation"

    def test_retry_value(self):
        """Test retry span kind value."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        assert PydanticAISpanKind.RETRY.value == "retry"

    def test_stream_value(self):
        """Test stream span kind value."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        assert PydanticAISpanKind.STREAM.value == "stream"


class TestPydanticAIAttributes:
    """Test PydanticAIAttributes class."""

    def test_span_kind_attribute(self):
        """Test span kind attribute."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.SPAN_KIND == "pydantic_ai.span_kind"

    def test_agent_attributes(self):
        """Test agent-level attributes."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.GEN_AI_AGENT_NAME == "pydantic_ai.agent.name"
        assert PydanticAIAttributes.AGENT_MODEL == "pydantic_ai.agent.model"
        assert PydanticAIAttributes.AGENT_INSTRUCTIONS == "pydantic_ai.agent.instructions"
        assert PydanticAIAttributes.AGENT_RESULT_TYPE == "pydantic_ai.agent.result_type"

    def test_run_attributes(self):
        """Test run-level attributes."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.RUN_ID == "pydantic_ai.run.id"
        assert PydanticAIAttributes.RUN_METHOD == "pydantic_ai.run.method"
        assert PydanticAIAttributes.RUN_PROMPT == "pydantic_ai.run.prompt"
        assert PydanticAIAttributes.RUN_RESULT == "pydantic_ai.run.result"

    def test_tool_attributes(self):
        """Test tool-level attributes."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.GEN_AI_TOOL_NAME == "pydantic_ai.tool.name"
        assert PydanticAIAttributes.TOOL_ARGS == "pydantic_ai.tool.args"
        assert PydanticAIAttributes.TOOL_RESULT == "pydantic_ai.tool.result"
        assert PydanticAIAttributes.TOOL_IS_ERROR == "pydantic_ai.tool.is_error"

    def test_usage_attributes(self):
        """Test usage/token attributes."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
        assert PydanticAIAttributes.USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
        assert PydanticAIAttributes.USAGE_TOTAL_TOKENS == "gen_ai.usage.total_tokens"

    def test_model_attributes(self):
        """Test model attributes."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.MODEL_NAME == "gen_ai.request.model"
        assert PydanticAIAttributes.MODEL_PROVIDER == "gen_ai.system"
        assert PydanticAIAttributes.MODEL_TEMPERATURE == "gen_ai.request.temperature"

    def test_error_attributes(self):
        """Test error attributes."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.ERROR_TYPE == "pydantic_ai.error.type"
        assert PydanticAIAttributes.ERROR_MESSAGE == "pydantic_ai.error.message"
        assert PydanticAIAttributes.IS_ERROR == "pydantic_ai.is_error"

    def test_retry_attributes(self):
        """Test retry attributes."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.RETRY_COUNT == "pydantic_ai.retry.count"
        assert PydanticAIAttributes.RETRY_REASON == "pydantic_ai.retry.reason"

    def test_stream_attributes(self):
        """Test streaming attributes."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert PydanticAIAttributes.STREAM_CHUNK_COUNT == "pydantic_ai.stream.chunk_count"
        assert PydanticAIAttributes.STREAM_IS_STRUCTURED == "pydantic_ai.stream.is_structured"


class TestGetModelProvider:
    """Test get_model_provider function."""

    def test_openai_models(self):
        """Test OpenAI model detection."""
        from traceai_pydantic_ai._attributes import get_model_provider

        assert get_model_provider("gpt-4o") == "openai"
        assert get_model_provider("gpt-4-turbo") == "openai"
        assert get_model_provider("o1-preview") == "openai"
        assert get_model_provider("o3-mini") == "openai"

    def test_anthropic_models(self):
        """Test Anthropic model detection."""
        from traceai_pydantic_ai._attributes import get_model_provider

        assert get_model_provider("claude-3-opus") == "anthropic"
        assert get_model_provider("claude-3-sonnet") == "anthropic"

    def test_google_models(self):
        """Test Google model detection."""
        from traceai_pydantic_ai._attributes import get_model_provider

        assert get_model_provider("gemini-pro") == "google"
        assert get_model_provider("models/gemini-1.5-pro") == "google"

    def test_provider_prefix_format(self):
        """Test provider:model format."""
        from traceai_pydantic_ai._attributes import get_model_provider

        assert get_model_provider("openai:gpt-4o") == "openai"
        assert get_model_provider("anthropic:claude-3-opus") == "anthropic"
        assert get_model_provider("groq:llama-3") == "groq"

    def test_other_providers(self):
        """Test other provider detection."""
        from traceai_pydantic_ai._attributes import get_model_provider

        assert get_model_provider("mistral-large") == "mistral"
        assert get_model_provider("deepseek-chat") == "deepseek"
        assert get_model_provider("groq-llama") == "groq"

    def test_unknown_model(self):
        """Test unknown model returns unknown."""
        from traceai_pydantic_ai._attributes import get_model_provider

        assert get_model_provider("some-random-model") == "unknown"

    def test_empty_model(self):
        """Test empty model name."""
        from traceai_pydantic_ai._attributes import get_model_provider

        assert get_model_provider("") == "unknown"
        assert get_model_provider(None) == "unknown"


class TestModelProviders:
    """Test MODEL_PROVIDERS mapping."""

    def test_providers_exist(self):
        """Test that model providers mapping exists."""
        from traceai_pydantic_ai._attributes import MODEL_PROVIDERS

        assert isinstance(MODEL_PROVIDERS, dict)
        assert len(MODEL_PROVIDERS) > 0

    def test_common_providers_mapped(self):
        """Test common providers are mapped."""
        from traceai_pydantic_ai._attributes import MODEL_PROVIDERS

        assert "openai" in MODEL_PROVIDERS
        assert "gpt-" in MODEL_PROVIDERS
        assert "claude-" in MODEL_PROVIDERS
        assert "gemini" in MODEL_PROVIDERS
