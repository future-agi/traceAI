"""Tests for the attributes module."""

import pytest
from unittest.mock import MagicMock

from traceai_strands._attributes import (
    SpanAttributes,
    get_model_provider,
    get_agent_attributes,
    get_tool_attributes,
    get_model_attributes,
    get_trace_attributes_from_config,
)


class TestGetModelProvider:
    """Tests for get_model_provider function."""

    def test_bedrock_model_with_region_prefix(self):
        """Test detecting Bedrock model with region prefix."""
        assert get_model_provider("us.anthropic.claude-sonnet-4-20250514-v1:0") == "bedrock"
        assert get_model_provider("eu.amazon.titan-embed-text-v2:0") == "bedrock"

    def test_openai_models(self):
        """Test detecting OpenAI models."""
        assert get_model_provider("gpt-4") == "openai"
        assert get_model_provider("gpt-4-turbo") == "openai"
        assert get_model_provider("o1-preview") == "openai"
        assert get_model_provider("text-embedding-ada-002") == "openai"

    def test_anthropic_models(self):
        """Test detecting Anthropic models."""
        assert get_model_provider("claude-3-opus") == "anthropic"
        assert get_model_provider("claude-3-sonnet") == "anthropic"
        assert get_model_provider("claude-2.1") == "anthropic"

    def test_google_models(self):
        """Test detecting Google models."""
        assert get_model_provider("gemini-pro") == "google"
        assert get_model_provider("gemini-1.5-flash") == "google"
        assert get_model_provider("palm-2") == "google"

    def test_mistral_models(self):
        """Test detecting Mistral models."""
        assert get_model_provider("mistral-large") == "mistral"
        assert get_model_provider("mixtral-8x7b") == "mistral"
        assert get_model_provider("codestral-latest") == "mistral"

    def test_meta_models(self):
        """Test detecting Meta models."""
        assert get_model_provider("llama-3.1-70b") == "meta"
        assert get_model_provider("llama-2-13b") == "meta"

    def test_ollama_models(self):
        """Test detecting Ollama models."""
        assert get_model_provider("ollama/llama3") == "ollama"

    def test_explicit_provider_prefix(self):
        """Test detecting provider from explicit prefix."""
        assert get_model_provider("azure/gpt-4") == "azure"
        assert get_model_provider("openai/gpt-4") == "openai"

    def test_unknown_model(self):
        """Test unknown model provider."""
        assert get_model_provider("my-custom-model") == "unknown"
        assert get_model_provider("") == "unknown"
        assert get_model_provider(None) == "unknown"


class TestGetAgentAttributes:
    """Tests for get_agent_attributes function."""

    def test_basic_agent_attributes(self, mock_agent):
        """Test extracting basic agent attributes."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.AGENT_TYPE] == "MagicMock"
        assert SpanAttributes.STRANDS_SYSTEM_PROMPT in attrs
        assert "helpful assistant" in attrs[SpanAttributes.STRANDS_SYSTEM_PROMPT]

    def test_agent_with_model(self, mock_agent):
        """Test extracting model information from agent."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "us.anthropic.claude-sonnet-4-20250514-v1:0"
        assert attrs[SpanAttributes.STRANDS_MODEL_PROVIDER] == "bedrock"
        assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == "bedrock"

    def test_agent_with_tools(self, mock_agent, mock_tool):
        """Test extracting tool count from agent."""
        mock_agent.tools = [mock_tool, mock_tool]
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.STRANDS_TOOL_COUNT] == 2

    def test_agent_with_trace_attributes(self, mock_agent):
        """Test extracting trace attributes."""
        mock_agent.trace_attributes = {
            "session.id": "sess-123",
            "user.id": "user@example.com",
        }
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.STRANDS_SESSION_ID] == "sess-123"
        assert attrs[SpanAttributes.STRANDS_USER_ID] == "user@example.com"

    def test_agent_with_long_system_prompt(self, mock_agent):
        """Test truncation of long system prompts."""
        mock_agent.system_prompt = "x" * 1000
        attrs = get_agent_attributes(mock_agent)

        assert len(attrs[SpanAttributes.STRANDS_SYSTEM_PROMPT]) <= 500
        assert attrs[SpanAttributes.STRANDS_SYSTEM_PROMPT].endswith("...")


class TestGetToolAttributes:
    """Tests for get_tool_attributes function."""

    def test_basic_tool_attributes(self, mock_tool):
        """Test extracting basic tool attributes."""
        attrs = get_tool_attributes(mock_tool)

        assert attrs[SpanAttributes.GEN_AI_TOOL_NAME] == "calculator"
        assert "sum of two numbers" in attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION]

    def test_tool_without_docstring(self):
        """Test tool without docstring."""
        def no_doc_tool():
            pass

        attrs = get_tool_attributes(no_doc_tool)

        assert attrs[SpanAttributes.GEN_AI_TOOL_NAME] == "no_doc_tool"
        # No description attribute since there's no docstring

    def test_tool_with_long_description(self):
        """Test truncation of long tool descriptions."""
        def long_doc_tool():
            """This is a very long description."""
            pass

        long_doc_tool.__doc__ = "x" * 1000

        attrs = get_tool_attributes(long_doc_tool)

        assert len(attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION]) <= 500
        assert attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION].endswith("...")


class TestGetModelAttributes:
    """Tests for get_model_attributes function."""

    def test_basic_model_attributes(self):
        """Test extracting basic model attributes."""
        model = MagicMock()
        model.model_id = "gpt-4"

        attrs = get_model_attributes(model)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4"
        assert attrs[SpanAttributes.STRANDS_MODEL_PROVIDER] == "openai"

    def test_model_with_parameters(self):
        """Test extracting model parameters."""
        model = MagicMock()
        model.model_id = "gpt-4"
        model.temperature = 0.7
        model.max_tokens = 1000
        model.top_p = 0.9

        attrs = get_model_attributes(model)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_TEMPERATURE] == 0.7
        assert attrs[SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS] == 1000
        assert attrs[SpanAttributes.GEN_AI_REQUEST_TOP_P] == 0.9

    def test_model_with_response_usage(self, mock_response):
        """Test extracting usage from response."""
        model = MagicMock()
        model.model_id = "gpt-4"

        attrs = get_model_attributes(model, mock_response)

        assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 100
        assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 50
        assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 150

    def test_model_with_cache_metrics(self):
        """Test extracting Bedrock cache metrics."""
        model = MagicMock()
        model.model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"

        response = MagicMock()
        response.usage = MagicMock()
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50
        response.usage.cache_read_input_tokens = 80
        response.usage.cache_creation_input_tokens = 20

        attrs = get_model_attributes(model, response)

        assert attrs[SpanAttributes.STRANDS_CACHE_READ_TOKENS] == 80
        assert attrs[SpanAttributes.STRANDS_CACHE_WRITE_TOKENS] == 20


class TestGetTraceAttributesFromConfig:
    """Tests for get_trace_attributes_from_config function."""

    def test_all_attributes(self):
        """Test creating trace attributes with all options."""
        attrs = get_trace_attributes_from_config(
            session_id="sess-123",
            user_id="user@example.com",
            tags=["production", "chatbot"],
            metadata={"env": "prod"},
        )

        assert attrs["session.id"] == "sess-123"
        assert attrs["user.id"] == "user@example.com"
        assert attrs["tags"] == ["production", "chatbot"]
        assert attrs["env"] == "prod"

    def test_partial_attributes(self):
        """Test creating trace attributes with partial options."""
        attrs = get_trace_attributes_from_config(session_id="sess-123")

        assert attrs["session.id"] == "sess-123"
        assert "user.id" not in attrs

    def test_empty_attributes(self):
        """Test creating trace attributes with no options."""
        attrs = get_trace_attributes_from_config()

        assert attrs == {}
