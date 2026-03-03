"""Tests for traceai_agno._attributes module."""

import pytest
from traceai_agno._attributes import (
    SpanAttributes,
    get_agent_attributes,
    get_tool_attributes,
    get_team_attributes,
    get_workflow_attributes,
    get_model_attributes,
    get_model_provider,
    create_trace_context,
    MODEL_PROVIDER_PATTERNS,
    _truncate,
)


class TestSpanAttributes:
    """Tests for SpanAttributes class."""

    def test_llm_attributes_exist(self):
        """Test LLM semantic attributes are defined."""
        assert SpanAttributes.GEN_AI_PROVIDER_NAME == "gen_ai.provider.name"
        assert SpanAttributes.GEN_AI_REQUEST_MODEL == "gen_ai.request.model"
        assert SpanAttributes.GEN_AI_RESPONSE_MODEL == "gen_ai.response.model"
        assert SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS == "gen_ai.request.max_tokens"
        assert SpanAttributes.GEN_AI_REQUEST_TEMPERATURE == "gen_ai.request.temperature"
        assert SpanAttributes.GEN_AI_REQUEST_TOP_P == "gen_ai.request.top_p"
        assert SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
        assert SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
        assert SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS == "gen_ai.usage.total_tokens"

    def test_agent_attributes_exist(self):
        """Test agent semantic attributes are defined."""
        assert SpanAttributes.GEN_AI_AGENT_NAME == "agent.name"
        assert SpanAttributes.AGENT_TYPE == "agent.type"
        assert SpanAttributes.AGENT_DESCRIPTION == "agent.description"
        assert SpanAttributes.AGENT_INSTRUCTIONS == "agent.instructions"

    def test_tool_attributes_exist(self):
        """Test tool semantic attributes are defined."""
        assert SpanAttributes.GEN_AI_TOOL_NAME == "gen_ai.tool.name"
        assert SpanAttributes.GEN_AI_TOOL_DESCRIPTION == "gen_ai.tool.description"
        assert SpanAttributes.TOOL_PARAMETERS == "gen_ai.tool.parameters"
        assert SpanAttributes.TOOL_RESULT == "gen_ai.tool.result"

    def test_agno_specific_attributes_exist(self):
        """Test Agno-specific attributes are defined."""
        assert SpanAttributes.AGNO_AGENT_ID == "agno.agent.id"
        assert SpanAttributes.AGNO_TOOL_COUNT == "agno.tool_count"
        assert SpanAttributes.AGNO_TEAM_NAME == "agno.team.name"
        assert SpanAttributes.AGNO_TEAM_MEMBERS == "agno.team.members"
        assert SpanAttributes.AGNO_WORKFLOW_NAME == "agno.workflow.name"
        assert SpanAttributes.AGNO_WORKFLOW_STEP == "agno.workflow.step"
        assert SpanAttributes.AGNO_SESSION_ID == "agno.session.id"
        assert SpanAttributes.AGNO_USER_ID == "agno.user.id"
        assert SpanAttributes.AGNO_DEBUG_MODE == "agno.debug_mode"
        assert SpanAttributes.AGNO_MARKDOWN == "agno.markdown"
        assert SpanAttributes.AGNO_MEMORY_ENABLED == "agno.memory.enabled"
        assert SpanAttributes.AGNO_KNOWLEDGE_ENABLED == "agno.knowledge.enabled"


class TestGetModelProvider:
    """Tests for get_model_provider function."""

    def test_openai_models(self):
        """Test OpenAI model detection."""
        assert get_model_provider("gpt-4") == "openai"
        assert get_model_provider("gpt-4-turbo") == "openai"
        assert get_model_provider("gpt-3.5-turbo") == "openai"
        assert get_model_provider("o1-preview") == "openai"
        assert get_model_provider("o3-mini") == "openai"
        assert get_model_provider("text-davinci-003") == "openai"
        assert get_model_provider("text-embedding-ada-002") == "openai"

    def test_anthropic_models(self):
        """Test Anthropic model detection."""
        assert get_model_provider("claude-3-5-sonnet-20241022") == "anthropic"
        assert get_model_provider("claude-3-opus") == "anthropic"
        assert get_model_provider("claude-2.1") == "anthropic"
        assert get_model_provider("claude-instant") == "anthropic"

    def test_google_models(self):
        """Test Google model detection."""
        assert get_model_provider("gemini-1.5-pro") == "google"
        assert get_model_provider("gemini-pro") == "google"
        assert get_model_provider("palm-2") == "google"
        assert get_model_provider("text-bison") == "google"

    def test_mistral_models(self):
        """Test Mistral model detection."""
        assert get_model_provider("mistral-large") == "mistral"
        assert get_model_provider("mistral-medium") == "mistral"
        assert get_model_provider("mixtral-8x7b") == "mistral"
        assert get_model_provider("codestral") == "mistral"

    def test_meta_models(self):
        """Test Meta model detection."""
        assert get_model_provider("llama-3.1-70b") == "meta"
        assert get_model_provider("llama-2-70b") == "meta"

    def test_cohere_models(self):
        """Test Cohere model detection."""
        assert get_model_provider("command-r-plus") == "cohere"
        assert get_model_provider("command-r") == "cohere"
        assert get_model_provider("embed-english-v3.0") == "cohere"

    def test_deepseek_models(self):
        """Test DeepSeek model detection."""
        assert get_model_provider("deepseek-chat") == "deepseek"
        assert get_model_provider("deepseek-coder") == "deepseek"

    def test_ollama_models(self):
        """Test Ollama model detection."""
        assert get_model_provider("ollama/llama3") == "ollama"
        assert get_model_provider("ollama/mistral") == "ollama"
        assert get_model_provider("ollama/codellama") == "ollama"

    def test_groq_models(self):
        """Test Groq model detection."""
        assert get_model_provider("groq/llama3-70b") == "groq"
        assert get_model_provider("groq/mixtral-8x7b") == "groq"

    def test_together_models(self):
        """Test Together AI model detection."""
        assert get_model_provider("together/llama-2-70b") == "together"
        assert get_model_provider("together/mixtral-8x7b") == "together"

    def test_fireworks_models(self):
        """Test Fireworks model detection."""
        assert get_model_provider("fireworks/llama-v3-70b") == "fireworks"

    def test_bedrock_models(self):
        """Test AWS Bedrock model detection."""
        assert get_model_provider("amazon.titan-text-express") == "bedrock"
        assert get_model_provider("ai21.jamba-instruct") == "bedrock"
        assert get_model_provider("stability.stable-diffusion-xl") == "bedrock"

    def test_azure_models(self):
        """Test Azure OpenAI model detection."""
        assert get_model_provider("azure/gpt-4") == "azure"
        assert get_model_provider("azure/gpt-35-turbo") == "azure"

    def test_explicit_prefix_detection(self):
        """Test that explicit prefixes are properly detected."""
        assert get_model_provider("openai/gpt-4") == "openai"
        assert get_model_provider("anthropic/claude-3") == "anthropic"
        assert get_model_provider("google/gemini-pro") == "google"
        assert get_model_provider("mistral/mistral-large") == "mistral"

    def test_unknown_models(self):
        """Test unknown model detection."""
        assert get_model_provider("unknown-model") == "unknown"
        assert get_model_provider("custom-llm") == "unknown"
        assert get_model_provider("") == "unknown"
        assert get_model_provider(None) == "unknown"


class TestGetAgentAttributes:
    """Tests for get_agent_attributes function."""

    def test_basic_agent_attributes(self, mock_agent):
        """Test basic agent attribute extraction."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.AGENT_TYPE] == "MockAgent"
        assert attrs[SpanAttributes.GEN_AI_AGENT_NAME] == "TestAgent"
        assert attrs[SpanAttributes.AGNO_AGENT_ID] == "agent-123"
        assert attrs[SpanAttributes.AGENT_DESCRIPTION] == "A test agent"
        assert attrs[SpanAttributes.AGENT_INSTRUCTIONS] == "Be helpful and accurate"

    def test_model_attributes(self, mock_agent):
        """Test model attribute extraction from agent."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4"
        assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == "openai"

    def test_tool_count(self, mock_agent_with_tools):
        """Test tool count extraction."""
        attrs = get_agent_attributes(mock_agent_with_tools)

        assert attrs[SpanAttributes.AGNO_TOOL_COUNT] == 2

    def test_debug_mode(self, mock_agent):
        """Test debug mode extraction."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.AGNO_DEBUG_MODE] is False

    def test_markdown_setting(self, mock_agent):
        """Test markdown setting extraction."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.AGNO_MARKDOWN] is True

    def test_memory_enabled(self, mock_agent):
        """Test memory enabled detection."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.AGNO_MEMORY_ENABLED] is False

    def test_knowledge_enabled(self, mock_agent):
        """Test knowledge enabled detection."""
        attrs = get_agent_attributes(mock_agent)

        assert attrs[SpanAttributes.AGNO_KNOWLEDGE_ENABLED] is False

    def test_agent_with_memory(self):
        """Test agent with memory enabled."""
        from tests.conftest import MockAgent

        agent = MockAgent(memory={"type": "summary"})
        attrs = get_agent_attributes(agent)

        assert attrs[SpanAttributes.AGNO_MEMORY_ENABLED] is True

    def test_agent_with_knowledge(self):
        """Test agent with knowledge enabled."""
        from tests.conftest import MockAgent

        agent = MockAgent(knowledge={"type": "vector_db"})
        attrs = get_agent_attributes(agent)

        assert attrs[SpanAttributes.AGNO_KNOWLEDGE_ENABLED] is True

    def test_anthropic_model(self, mock_agent_with_tools):
        """Test Anthropic model detection in agent."""
        attrs = get_agent_attributes(mock_agent_with_tools)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "claude-3-5-sonnet-20241022"
        assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == "anthropic"

    def test_agent_without_optional_fields(self):
        """Test agent with minimal attributes."""

        class MinimalAgent:
            pass

        agent = MinimalAgent()
        attrs = get_agent_attributes(agent)

        assert attrs[SpanAttributes.AGENT_TYPE] == "MinimalAgent"
        assert attrs[SpanAttributes.AGNO_TOOL_COUNT] == 0
        assert attrs[SpanAttributes.AGNO_MEMORY_ENABLED] is False
        assert attrs[SpanAttributes.AGNO_KNOWLEDGE_ENABLED] is False


class TestGetToolAttributes:
    """Tests for get_tool_attributes function."""

    def test_basic_tool_attributes(self, mock_tool):
        """Test basic tool attribute extraction."""
        attrs = get_tool_attributes(mock_tool)

        assert attrs[SpanAttributes.GEN_AI_TOOL_NAME] == "test_tool"
        assert attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION] == "A test tool"

    def test_tool_with_long_description(self):
        """Test tool with description that needs truncation."""
        from tests.conftest import MockTool

        long_description = "A" * 600
        tool = MockTool(name="long_tool", description=long_description)
        attrs = get_tool_attributes(tool)

        assert len(attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION]) == 500
        assert attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION].endswith("...")

    def test_tool_without_name(self):
        """Test tool without name attribute."""

        class NamelessTool:
            description = "Some description"

        tool = NamelessTool()
        attrs = get_tool_attributes(tool)

        assert SpanAttributes.GEN_AI_TOOL_NAME not in attrs
        assert attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION] == "Some description"

    def test_tool_with_function_name(self):
        """Test tool using __name__ for name."""

        def my_tool():
            """Tool description from docstring."""
            pass

        attrs = get_tool_attributes(my_tool)

        assert attrs[SpanAttributes.GEN_AI_TOOL_NAME] == "my_tool"
        assert attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION] == "Tool description from docstring."


class TestGetTeamAttributes:
    """Tests for get_team_attributes function."""

    def test_basic_team_attributes(self, mock_team):
        """Test basic team attribute extraction."""
        attrs = get_team_attributes(mock_team)

        assert attrs[SpanAttributes.AGNO_TEAM_NAME] == "TestTeam"
        assert attrs[SpanAttributes.AGNO_TEAM_MEMBERS] == "Agent1, Agent2"

    def test_empty_team(self):
        """Test team with no members."""
        from tests.conftest import MockTeam

        team = MockTeam(name="EmptyTeam", agents=[])
        attrs = get_team_attributes(team)

        assert attrs[SpanAttributes.AGNO_TEAM_NAME] == "EmptyTeam"
        assert SpanAttributes.AGNO_TEAM_MEMBERS not in attrs

    def test_team_without_name(self):
        """Test team without name attribute."""

        class NamelessTeam:
            agents = []

        team = NamelessTeam()
        attrs = get_team_attributes(team)

        assert SpanAttributes.AGNO_TEAM_NAME not in attrs


class TestGetWorkflowAttributes:
    """Tests for get_workflow_attributes function."""

    def test_basic_workflow_attributes(self, mock_workflow):
        """Test basic workflow attribute extraction."""
        attrs = get_workflow_attributes(mock_workflow)

        assert attrs[SpanAttributes.AGNO_WORKFLOW_NAME] == "TestWorkflow"

    def test_workflow_without_name(self):
        """Test workflow without name attribute."""

        class NamelessWorkflow:
            pass

        workflow = NamelessWorkflow()
        attrs = get_workflow_attributes(workflow)

        assert SpanAttributes.AGNO_WORKFLOW_NAME not in attrs

    def test_workflow_with_function_name(self):
        """Test workflow using __name__ for name."""

        def my_workflow():
            pass

        attrs = get_workflow_attributes(my_workflow)

        assert attrs[SpanAttributes.AGNO_WORKFLOW_NAME] == "my_workflow"


class TestGetModelAttributes:
    """Tests for get_model_attributes function."""

    def test_basic_model_attributes(self, mock_model):
        """Test basic model attribute extraction."""
        attrs = get_model_attributes(mock_model)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4"
        assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == "openai"
        assert attrs[SpanAttributes.GEN_AI_REQUEST_TEMPERATURE] == 0.7
        assert attrs[SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS] == 1000
        assert attrs[SpanAttributes.GEN_AI_REQUEST_TOP_P] == 1.0

    def test_model_with_response_usage(self, mock_model, mock_response):
        """Test model attributes with response usage."""
        attrs = get_model_attributes(mock_model, mock_response)

        assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 100
        assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 50
        assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 150

    def test_model_without_optional_params(self):
        """Test model without optional parameters."""

        class MinimalModel:
            id = "test-model"

        model = MinimalModel()
        attrs = get_model_attributes(model)

        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "test-model"
        assert SpanAttributes.GEN_AI_REQUEST_TEMPERATURE not in attrs
        assert SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS not in attrs

    def test_response_with_prompt_tokens(self):
        """Test response using prompt_tokens instead of input_tokens."""

        class AltUsage:
            prompt_tokens = 200
            completion_tokens = 100

        class AltResponse:
            usage = AltUsage()

        class Model:
            id = "gpt-4"

        attrs = get_model_attributes(Model(), AltResponse())

        assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 200
        assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 100
        assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 300


class TestCreateTraceContext:
    """Tests for create_trace_context function."""

    def test_full_context(self):
        """Test creating full trace context."""
        context = create_trace_context(
            session_id="session-123",
            user_id="user-456",
            tags=["production", "test"],
            metadata={"environment": "prod", "version": "1.0"},
        )

        assert context[SpanAttributes.AGNO_SESSION_ID] == "session-123"
        assert context[SpanAttributes.AGNO_USER_ID] == "user-456"
        assert context["tags"] == ["production", "test"]
        assert context["environment"] == "prod"
        assert context["version"] == "1.0"

    def test_partial_context(self):
        """Test creating partial trace context."""
        context = create_trace_context(session_id="session-123")

        assert context[SpanAttributes.AGNO_SESSION_ID] == "session-123"
        assert SpanAttributes.AGNO_USER_ID not in context
        assert "tags" not in context

    def test_empty_context(self):
        """Test creating empty trace context."""
        context = create_trace_context()

        assert context == {}

    def test_only_metadata(self):
        """Test context with only metadata."""
        context = create_trace_context(metadata={"key": "value"})

        assert context == {"key": "value"}


class TestTruncate:
    """Tests for _truncate helper function."""

    def test_short_text(self):
        """Test text shorter than max length."""
        text = "Short text"
        result = _truncate(text, 500)
        assert result == text

    def test_exact_length(self):
        """Test text exactly at max length."""
        text = "A" * 500
        result = _truncate(text, 500)
        assert result == text

    def test_long_text(self):
        """Test text longer than max length."""
        text = "A" * 600
        result = _truncate(text, 500)
        assert len(result) == 500
        assert result.endswith("...")

    def test_custom_max_length(self):
        """Test with custom max length."""
        text = "A" * 100
        result = _truncate(text, 50)
        assert len(result) == 50
        assert result.endswith("...")


class TestModelProviderPatterns:
    """Tests for MODEL_PROVIDER_PATTERNS ordering."""

    def test_patterns_are_list_of_tuples(self):
        """Test that patterns are ordered list of tuples."""
        assert isinstance(MODEL_PROVIDER_PATTERNS, list)
        for item in MODEL_PROVIDER_PATTERNS:
            assert isinstance(item, tuple)
            assert len(item) == 2
            provider, patterns = item
            assert isinstance(provider, str)
            assert isinstance(patterns, list)

    def test_specific_patterns_before_generic(self):
        """Test that specific patterns come before generic ones."""
        # Find positions of specific vs generic providers
        provider_positions = {
            provider: idx for idx, (provider, _) in enumerate(MODEL_PROVIDER_PATTERNS)
        }

        # Mistral should come before bedrock
        assert provider_positions["mistral"] < provider_positions["bedrock"]

        # Meta should come before bedrock (llama patterns)
        assert provider_positions["meta"] < provider_positions["bedrock"]
