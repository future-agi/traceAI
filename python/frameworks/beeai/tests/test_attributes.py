"""Tests for the attributes module."""

import pytest
from unittest.mock import MagicMock

from traceai_beeai._attributes import (
    SpanAttributes,
    get_model_provider,
    get_agent_attributes,
    get_tool_attributes,
    get_workflow_attributes,
    get_model_attributes,
    create_trace_context,
)


class TestGetModelProvider:
    """Tests for get_model_provider function."""

    def test_ibm_granite_models(self):
        """Test detecting IBM Granite models."""
        assert get_model_provider("granite-3.1-8b-instruct") == "ibm"
        assert get_model_provider("granite-3.0-dense") == "ibm"
        assert get_model_provider("ibm/granite-3.1") == "ibm"

    def test_openai_models(self):
        """Test detecting OpenAI models."""
        assert get_model_provider("gpt-4") == "openai"
        assert get_model_provider("gpt-4-turbo") == "openai"
        assert get_model_provider("o1-preview") == "openai"

    def test_anthropic_models(self):
        """Test detecting Anthropic models."""
        assert get_model_provider("claude-3-opus") == "anthropic"
        assert get_model_provider("claude-3-sonnet") == "anthropic"

    def test_meta_models(self):
        """Test detecting Meta Llama models."""
        assert get_model_provider("llama-3.1-70b") == "meta"
        assert get_model_provider("llama-3.3-70b-instruct") == "meta"

    def test_google_models(self):
        """Test detecting Google models."""
        assert get_model_provider("gemini-pro") == "google"
        assert get_model_provider("gemini-1.5-flash") == "google"

    def test_mistral_models(self):
        """Test detecting Mistral models."""
        assert get_model_provider("mistral-large") == "mistral"
        assert get_model_provider("mixtral-8x7b") == "mistral"

    def test_ollama_models(self):
        """Test detecting Ollama models."""
        assert get_model_provider("ollama/llama3") == "ollama"

    def test_groq_models(self):
        """Test detecting Groq models."""
        assert get_model_provider("groq/llama-3.1-70b") == "groq"

    def test_together_models(self):
        """Test detecting Together AI models."""
        assert get_model_provider("together/meta-llama") == "together"

    def test_explicit_provider_prefix(self):
        """Test detecting provider from explicit prefix."""
        assert get_model_provider("azure/gpt-4") == "azure"
        assert get_model_provider("bedrock/anthropic.claude") == "bedrock"

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
        assert attrs[SpanAttributes.GEN_AI_AGENT_NAME] == "test_agent"
        assert attrs[SpanAttributes.AGENT_ROLE] == "Assistant"

    def test_agent_with_model(self, mock_agent):
        """Test extracting model information from agent."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "granite-3.1-8b-instruct"
        assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == "ibm"

    def test_agent_with_tools(self, mock_agent, mock_tool):
        """Test extracting tool count from agent."""
        mock_agent.tools = [mock_tool, mock_tool]
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.BEEAI_TOOL_COUNT] == 2

    def test_agent_with_requirements(self, mock_agent):
        """Test extracting requirements from agent."""
        req1 = MagicMock()
        req1.name = "SafetyRequirement"
        req2 = MagicMock()
        req2.name = "OutputRequirement"
        mock_agent.requirements = [req1, req2]

        attrs = get_agent_attributes(mock_agent)

        assert "SafetyRequirement" in attrs[SpanAttributes.BEEAI_REQUIREMENTS]
        assert "OutputRequirement" in attrs[SpanAttributes.BEEAI_REQUIREMENTS]

    def test_agent_with_memory(self, mock_agent):
        """Test extracting memory type from agent."""
        mock_agent.memory = MagicMock()
        type(mock_agent.memory).__name__ = "ConversationMemory"

        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.BEEAI_MEMORY_TYPE] == "ConversationMemory"

    def test_agent_with_long_instructions(self, mock_agent):
        """Test truncation of long instructions."""
        mock_agent.instructions = "x" * 1000
        attrs = get_agent_attributes(mock_agent)

        assert len(attrs[SpanAttributes.AGENT_INSTRUCTIONS]) <= 500
        assert attrs[SpanAttributes.AGENT_INSTRUCTIONS].endswith("...")


class TestGetToolAttributes:
    """Tests for get_tool_attributes function."""

    def test_basic_tool_attributes(self, mock_tool):
        """Test extracting basic tool attributes."""
        attrs = get_tool_attributes(mock_tool)

        assert attrs[SpanAttributes.GEN_AI_TOOL_NAME] == "calculator"
        assert "mathematical calculations" in attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION]

    def test_tool_without_description(self):
        """Test tool without description."""
        tool = MagicMock()
        tool.name = "simple_tool"
        tool.description = None
        tool.__doc__ = None

        attrs = get_tool_attributes(tool)

        assert attrs[SpanAttributes.GEN_AI_TOOL_NAME] == "simple_tool"
        assert SpanAttributes.GEN_AI_TOOL_DESCRIPTION not in attrs

    def test_tool_with_long_description(self):
        """Test truncation of long tool descriptions."""
        tool = MagicMock()
        tool.name = "verbose_tool"
        tool.description = "x" * 1000

        attrs = get_tool_attributes(tool)

        assert len(attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION]) <= 500
        assert attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION].endswith("...")


class TestGetWorkflowAttributes:
    """Tests for get_workflow_attributes function."""

    def test_workflow_with_name(self):
        """Test extracting workflow name."""
        workflow = MagicMock()
        workflow.name = "data_pipeline"

        attrs = get_workflow_attributes(workflow)

        assert attrs[SpanAttributes.BEEAI_WORKFLOW_NAME] == "data_pipeline"

    def test_workflow_without_name(self):
        """Test workflow without name attribute."""
        workflow = MagicMock(spec=[])
        workflow.__name__ = "ProcessWorkflow"

        attrs = get_workflow_attributes(workflow)

        assert attrs[SpanAttributes.BEEAI_WORKFLOW_NAME] == "ProcessWorkflow"


class TestGetModelAttributes:
    """Tests for get_model_attributes function."""

    def test_basic_model_attributes(self):
        """Test extracting basic model attributes."""
        model = MagicMock()
        model.model = "gpt-4"

        attrs = get_model_attributes(model)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4"
        assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == "openai"

    def test_model_with_parameters(self):
        """Test extracting model parameters."""
        model = MagicMock()
        model.model = "gpt-4"
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
        model.model = "gpt-4"

        attrs = get_model_attributes(model, mock_response)

        assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 100
        assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 50
        assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 150


class TestCreateTraceContext:
    """Tests for create_trace_context function."""

    def test_all_attributes(self):
        """Test creating trace context with all options."""
        context = create_trace_context(
            session_id="sess-123",
            user_id="user@example.com",
            tags=["production", "chatbot"],
            metadata={"env": "prod"},
        )

        assert context[SpanAttributes.BEEAI_SESSION_ID] == "sess-123"
        assert context[SpanAttributes.BEEAI_USER_ID] == "user@example.com"
        assert context["tags"] == ["production", "chatbot"]
        assert context["env"] == "prod"

    def test_partial_attributes(self):
        """Test creating trace context with partial options."""
        context = create_trace_context(session_id="sess-123")

        assert context[SpanAttributes.BEEAI_SESSION_ID] == "sess-123"
        assert SpanAttributes.BEEAI_USER_ID not in context

    def test_empty_context(self):
        """Test creating empty trace context."""
        context = create_trace_context()

        assert context == {}
