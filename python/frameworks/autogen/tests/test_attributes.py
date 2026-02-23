"""Tests for AutoGen attributes module."""

import pytest

from traceai_autogen._attributes import (
    AutoGenSpanKind,
    AutoGenAttributes,
    MODEL_PROVIDERS,
    get_model_provider,
    get_agent_type_name,
    get_team_type_name,
)


class TestAutoGenSpanKind:
    """Tests for AutoGenSpanKind enum."""

    def test_agent_span_kinds(self):
        """Test agent-level span kinds."""
        assert AutoGenSpanKind.AGENT_RUN == "agent_run"
        assert AutoGenSpanKind.AGENT_CALL == "agent_call"

    def test_team_span_kinds(self):
        """Test team-level span kinds."""
        assert AutoGenSpanKind.TEAM_RUN == "team_run"
        assert AutoGenSpanKind.TEAM_ROUND == "team_round"

    def test_message_span_kinds(self):
        """Test message-level span kinds."""
        assert AutoGenSpanKind.MESSAGE_SEND == "message_send"
        assert AutoGenSpanKind.MESSAGE_RECEIVE == "message_receive"

    def test_tool_span_kinds(self):
        """Test tool-related span kinds."""
        assert AutoGenSpanKind.TOOL_CALL == "tool_call"
        assert AutoGenSpanKind.TOOL_RESULT == "tool_result"

    def test_model_span_kinds(self):
        """Test model/LLM span kinds."""
        assert AutoGenSpanKind.MODEL_REQUEST == "model_request"
        assert AutoGenSpanKind.MODEL_STREAM == "model_stream"

    def test_other_span_kinds(self):
        """Test other span kinds."""
        assert AutoGenSpanKind.CODE_EXECUTION == "code_execution"
        assert AutoGenSpanKind.HANDOFF == "handoff"

    def test_enum_is_string(self):
        """Test that enum values are strings."""
        for kind in AutoGenSpanKind:
            assert isinstance(kind.value, str)
            assert isinstance(kind, str)


class TestAutoGenAttributes:
    """Tests for AutoGenAttributes class."""

    def test_span_kind_attribute(self):
        """Test span kind attribute."""
        assert AutoGenAttributes.SPAN_KIND == "autogen.span_kind"

    def test_agent_attributes(self):
        """Test agent-related attributes."""
        assert AutoGenAttributes.GEN_AI_AGENT_NAME == "autogen.agent.name"
        assert AutoGenAttributes.AGENT_TYPE == "autogen.agent.type"
        assert AutoGenAttributes.AGENT_DESCRIPTION == "autogen.agent.description"
        assert AutoGenAttributes.AGENT_SYSTEM_MESSAGE == "autogen.agent.system_message"
        assert AutoGenAttributes.AGENT_TOOL_COUNT == "autogen.agent.tool_count"
        assert AutoGenAttributes.AGENT_HAS_MEMORY == "autogen.agent.has_memory"

    def test_team_attributes(self):
        """Test team-related attributes."""
        assert AutoGenAttributes.TEAM_NAME == "autogen.team.name"
        assert AutoGenAttributes.TEAM_TYPE == "autogen.team.type"
        assert AutoGenAttributes.TEAM_PARTICIPANT_COUNT == "autogen.team.participant_count"
        assert AutoGenAttributes.TEAM_PARTICIPANTS == "autogen.team.participants"
        assert AutoGenAttributes.TEAM_MAX_TURNS == "autogen.team.max_turns"
        assert AutoGenAttributes.TEAM_TERMINATION_CONDITION == "autogen.team.termination_condition"

    def test_task_attributes(self):
        """Test task/run attributes."""
        assert AutoGenAttributes.TASK_ID == "autogen.task.id"
        assert AutoGenAttributes.TASK_CONTENT == "autogen.task.content"
        assert AutoGenAttributes.TASK_RESULT == "autogen.task.result"
        assert AutoGenAttributes.TASK_STOP_REASON == "autogen.task.stop_reason"
        assert AutoGenAttributes.TASK_MESSAGE_COUNT == "autogen.task.message_count"
        assert AutoGenAttributes.TASK_TURN_COUNT == "autogen.task.turn_count"

    def test_message_attributes(self):
        """Test message attributes."""
        assert AutoGenAttributes.MESSAGE_TYPE == "autogen.message.type"
        assert AutoGenAttributes.MESSAGE_CONTENT == "autogen.message.content"
        assert AutoGenAttributes.MESSAGE_SOURCE == "autogen.message.source"
        assert AutoGenAttributes.MESSAGE_MODELS_USAGE == "autogen.message.models_usage"

    def test_tool_attributes(self):
        """Test tool-related attributes."""
        assert AutoGenAttributes.GEN_AI_TOOL_NAME == "autogen.tool.name"
        assert AutoGenAttributes.GEN_AI_TOOL_DESCRIPTION == "autogen.tool.description"
        assert AutoGenAttributes.TOOL_ARGS == "autogen.tool.args"
        assert AutoGenAttributes.TOOL_RESULT == "autogen.tool.result"
        assert AutoGenAttributes.TOOL_IS_ERROR == "autogen.tool.is_error"
        assert AutoGenAttributes.TOOL_ERROR_MESSAGE == "autogen.tool.error_message"
        assert AutoGenAttributes.TOOL_DURATION_MS == "autogen.tool.duration_ms"
        assert AutoGenAttributes.TOOL_CALL_ID == "autogen.tool.call_id"

    def test_model_attributes_follow_genai_conventions(self):
        """Test that model attributes follow GenAI semantic conventions."""
        assert AutoGenAttributes.MODEL_NAME == "gen_ai.request.model"
        assert AutoGenAttributes.MODEL_PROVIDER == "gen_ai.system"
        assert AutoGenAttributes.MODEL_TEMPERATURE == "gen_ai.request.temperature"
        assert AutoGenAttributes.MODEL_MAX_TOKENS == "gen_ai.request.max_tokens"
        assert AutoGenAttributes.MODEL_TOP_P == "gen_ai.request.top_p"

    def test_usage_attributes_follow_genai_conventions(self):
        """Test that usage attributes follow GenAI semantic conventions."""
        assert AutoGenAttributes.USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
        assert AutoGenAttributes.USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
        assert AutoGenAttributes.USAGE_TOTAL_TOKENS == "gen_ai.usage.total_tokens"
        assert AutoGenAttributes.USAGE_PROMPT_TOKENS == "gen_ai.usage.prompt_tokens"
        assert AutoGenAttributes.USAGE_COMPLETION_TOKENS == "gen_ai.usage.completion_tokens"

    def test_handoff_attributes(self):
        """Test handoff-related attributes."""
        assert AutoGenAttributes.HANDOFF_TARGET == "autogen.handoff.target"
        assert AutoGenAttributes.HANDOFF_CONTENT == "autogen.handoff.content"
        assert AutoGenAttributes.HANDOFF_SOURCE == "autogen.handoff.source"

    def test_code_execution_attributes(self):
        """Test code execution attributes."""
        assert AutoGenAttributes.CODE_LANGUAGE == "autogen.code.language"
        assert AutoGenAttributes.CODE_CONTENT == "autogen.code.content"
        assert AutoGenAttributes.CODE_OUTPUT == "autogen.code.output"
        assert AutoGenAttributes.CODE_EXIT_CODE == "autogen.code.exit_code"
        assert AutoGenAttributes.CODE_EXECUTOR_TYPE == "autogen.code.executor_type"

    def test_error_attributes(self):
        """Test error-related attributes."""
        assert AutoGenAttributes.ERROR_TYPE == "autogen.error.type"
        assert AutoGenAttributes.ERROR_MESSAGE == "autogen.error.message"
        assert AutoGenAttributes.IS_ERROR == "autogen.is_error"

    def test_performance_attributes(self):
        """Test performance attributes."""
        assert AutoGenAttributes.DURATION_MS == "autogen.duration_ms"
        assert AutoGenAttributes.TIME_TO_FIRST_TOKEN_MS == "autogen.time_to_first_token_ms"

    def test_round_attributes(self):
        """Test round/turn attributes."""
        assert AutoGenAttributes.ROUND_NUMBER == "autogen.round.number"
        assert AutoGenAttributes.ROUND_SPEAKER == "autogen.round.speaker"

    def test_streaming_attributes(self):
        """Test streaming attributes."""
        assert AutoGenAttributes.STREAM_CHUNK_COUNT == "autogen.stream.chunk_count"
        assert AutoGenAttributes.STREAM_IS_COMPLETE == "autogen.stream.is_complete"

    def test_memory_attributes(self):
        """Test memory attributes."""
        assert AutoGenAttributes.MEMORY_TYPE == "autogen.memory.type"
        assert AutoGenAttributes.MEMORY_OPERATION == "autogen.memory.operation"

    def test_metadata_attribute(self):
        """Test metadata attribute."""
        assert AutoGenAttributes.METADATA == "autogen.metadata"


class TestModelProviders:
    """Tests for MODEL_PROVIDERS mapping."""

    def test_openai_providers(self):
        """Test OpenAI provider mappings."""
        assert MODEL_PROVIDERS["openai"] == "openai"
        assert MODEL_PROVIDERS["gpt-"] == "openai"
        assert MODEL_PROVIDERS["o1-"] == "openai"
        assert MODEL_PROVIDERS["o3-"] == "openai"

    def test_anthropic_provider(self):
        """Test Anthropic provider mapping."""
        assert MODEL_PROVIDERS["anthropic"] == "anthropic"
        assert MODEL_PROVIDERS["claude-"] == "anthropic"

    def test_google_provider(self):
        """Test Google provider mapping."""
        assert MODEL_PROVIDERS["gemini"] == "google"
        assert MODEL_PROVIDERS["models/gemini"] == "google"

    def test_other_providers(self):
        """Test other provider mappings."""
        assert MODEL_PROVIDERS["groq"] == "groq"
        assert MODEL_PROVIDERS["mistral"] == "mistral"
        assert MODEL_PROVIDERS["deepseek"] == "deepseek"
        assert MODEL_PROVIDERS["cohere"] == "cohere"
        assert MODEL_PROVIDERS["azure"] == "azure"
        assert MODEL_PROVIDERS["ollama"] == "ollama"
        assert MODEL_PROVIDERS["together"] == "together"


class TestGetModelProvider:
    """Tests for get_model_provider function."""

    def test_empty_model_name(self):
        """Test with empty model name."""
        assert get_model_provider("") == "unknown"
        assert get_model_provider(None) == "unknown"

    def test_openai_models(self):
        """Test OpenAI model detection."""
        assert get_model_provider("gpt-4") == "openai"
        assert get_model_provider("gpt-4o") == "openai"
        assert get_model_provider("gpt-3.5-turbo") == "openai"
        assert get_model_provider("o1-preview") == "openai"
        assert get_model_provider("o3-mini") == "openai"

    def test_anthropic_models(self):
        """Test Anthropic model detection."""
        assert get_model_provider("claude-3-opus") == "anthropic"
        assert get_model_provider("claude-3-sonnet") == "anthropic"
        assert get_model_provider("claude-3.5-sonnet") == "anthropic"

    def test_google_models(self):
        """Test Google model detection."""
        assert get_model_provider("gemini-pro") == "google"
        assert get_model_provider("gemini-1.5-flash") == "google"
        assert get_model_provider("models/gemini-pro") == "google"

    def test_other_providers(self):
        """Test other provider detection."""
        assert get_model_provider("mistral-large") == "mistral"
        assert get_model_provider("deepseek-coder") == "deepseek"
        assert get_model_provider("groq/llama-70b") == "groq"
        assert get_model_provider("ollama/llama2") == "ollama"

    def test_provider_prefix_format(self):
        """Test provider:model format."""
        assert get_model_provider("openai:gpt-4") == "openai"
        assert get_model_provider("anthropic:claude-3") == "anthropic"
        assert get_model_provider("custom:my-model") == "custom"

    def test_provider_slash_format(self):
        """Test provider/model format."""
        assert get_model_provider("openai/gpt-4") == "openai"
        assert get_model_provider("anthropic/claude-3") == "anthropic"

    def test_unknown_provider(self):
        """Test unknown provider detection."""
        assert get_model_provider("random-model-name") == "unknown"
        assert get_model_provider("my-custom-model") == "unknown"

    def test_case_insensitive(self):
        """Test case insensitivity."""
        assert get_model_provider("GPT-4") == "openai"
        assert get_model_provider("Claude-3") == "anthropic"
        assert get_model_provider("GEMINI-PRO") == "google"


class TestGetAgentTypeName:
    """Tests for get_agent_type_name function."""

    def test_with_class_instance(self):
        """Test with actual class instance."""
        class MyAgent:
            pass

        agent = MyAgent()
        assert get_agent_type_name(agent) == "MyAgent"

    def test_with_mock(self):
        """Test with mock object."""
        from unittest.mock import MagicMock

        agent = MagicMock()
        assert "MagicMock" in get_agent_type_name(agent) or "Mock" in get_agent_type_name(agent)


class TestGetTeamTypeName:
    """Tests for get_team_type_name function."""

    def test_with_class_instance(self):
        """Test with actual class instance."""
        class RoundRobinGroupChat:
            pass

        team = RoundRobinGroupChat()
        assert get_team_type_name(team) == "RoundRobinGroupChat"

    def test_with_mock(self):
        """Test with mock object."""
        from unittest.mock import MagicMock

        team = MagicMock()
        assert "MagicMock" in get_team_type_name(team) or "Mock" in get_team_type_name(team)
