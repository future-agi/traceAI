"""Semantic attributes for AutoGen instrumentation.

These attributes follow OpenTelemetry semantic conventions where applicable,
with AutoGen-specific extensions for multi-agent patterns.
"""

from enum import Enum


class AutoGenSpanKind(str, Enum):
    """Span kinds for AutoGen operations."""

    # Agent-level spans
    AGENT_RUN = "agent_run"  # Agent processing messages
    AGENT_CALL = "agent_call"  # Direct agent invocation

    # Team-level spans
    TEAM_RUN = "team_run"  # Team task execution
    TEAM_ROUND = "team_round"  # Single round in group chat

    # Message-level spans
    MESSAGE_SEND = "message_send"  # Message sent
    MESSAGE_RECEIVE = "message_receive"  # Message received

    # Tool spans
    TOOL_CALL = "tool_call"  # Tool/function execution
    TOOL_RESULT = "tool_result"  # Tool result handling

    # LLM spans
    MODEL_REQUEST = "model_request"  # LLM model request
    MODEL_STREAM = "model_stream"  # Streaming response

    # Code execution
    CODE_EXECUTION = "code_execution"  # Code executor run

    # Handoff
    HANDOFF = "handoff"  # Agent handoff


class AutoGenAttributes:
    """Semantic attributes for AutoGen spans.

    Follows OpenTelemetry GenAI semantic conventions where applicable.
    """

    # =========================================================================
    # Span Kind
    # =========================================================================
    SPAN_KIND = "autogen.span_kind"

    # =========================================================================
    # Agent Attributes
    # =========================================================================
    GEN_AI_AGENT_NAME = "autogen.agent.name"
    AGENT_TYPE = "autogen.agent.type"  # AssistantAgent, CodeExecutorAgent, etc.
    AGENT_DESCRIPTION = "autogen.agent.description"
    AGENT_SYSTEM_MESSAGE = "autogen.agent.system_message"
    AGENT_TOOL_COUNT = "autogen.agent.tool_count"
    AGENT_HAS_MEMORY = "autogen.agent.has_memory"

    # =========================================================================
    # Team Attributes
    # =========================================================================
    TEAM_NAME = "autogen.team.name"
    TEAM_TYPE = "autogen.team.type"  # RoundRobinGroupChat, SelectorGroupChat, etc.
    TEAM_PARTICIPANT_COUNT = "autogen.team.participant_count"
    TEAM_PARTICIPANTS = "autogen.team.participants"  # JSON list of agent names
    TEAM_MAX_TURNS = "autogen.team.max_turns"
    TEAM_TERMINATION_CONDITION = "autogen.team.termination_condition"

    # =========================================================================
    # Task/Run Attributes
    # =========================================================================
    TASK_ID = "autogen.task.id"
    TASK_CONTENT = "autogen.task.content"
    TASK_RESULT = "autogen.task.result"
    TASK_STOP_REASON = "autogen.task.stop_reason"
    TASK_MESSAGE_COUNT = "autogen.task.message_count"
    TASK_TURN_COUNT = "autogen.task.turn_count"

    # =========================================================================
    # Message Attributes
    # =========================================================================
    MESSAGE_TYPE = "autogen.message.type"  # TextMessage, ToolCallSummaryMessage, etc.
    MESSAGE_CONTENT = "autogen.message.content"
    MESSAGE_SOURCE = "autogen.message.source"  # Agent name that sent
    MESSAGE_MODELS_USAGE = "autogen.message.models_usage"

    # =========================================================================
    # Tool Attributes
    # =========================================================================
    GEN_AI_TOOL_NAME = "autogen.tool.name"
    GEN_AI_TOOL_DESCRIPTION = "autogen.tool.description"
    TOOL_ARGS = "autogen.tool.args"
    TOOL_RESULT = "autogen.tool.result"
    TOOL_IS_ERROR = "autogen.tool.is_error"
    TOOL_ERROR_MESSAGE = "autogen.tool.error_message"
    TOOL_DURATION_MS = "autogen.tool.duration_ms"
    TOOL_CALL_ID = "autogen.tool.call_id"

    # =========================================================================
    # Model/LLM Attributes (GenAI Semantic Conventions)
    # =========================================================================
    MODEL_NAME = "gen_ai.request.model"
    MODEL_PROVIDER = "gen_ai.system"
    MODEL_TEMPERATURE = "gen_ai.request.temperature"
    MODEL_MAX_TOKENS = "gen_ai.request.max_tokens"
    MODEL_TOP_P = "gen_ai.request.top_p"

    # =========================================================================
    # Usage Attributes (GenAI Semantic Conventions)
    # =========================================================================
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
    USAGE_PROMPT_TOKENS = "gen_ai.usage.prompt_tokens"
    USAGE_COMPLETION_TOKENS = "gen_ai.usage.completion_tokens"

    # =========================================================================
    # Handoff Attributes
    # =========================================================================
    HANDOFF_TARGET = "autogen.handoff.target"
    HANDOFF_CONTENT = "autogen.handoff.content"
    HANDOFF_SOURCE = "autogen.handoff.source"

    # =========================================================================
    # Code Execution Attributes
    # =========================================================================
    CODE_LANGUAGE = "autogen.code.language"
    CODE_CONTENT = "autogen.code.content"
    CODE_OUTPUT = "autogen.code.output"
    CODE_EXIT_CODE = "autogen.code.exit_code"
    CODE_EXECUTOR_TYPE = "autogen.code.executor_type"

    # =========================================================================
    # Error Attributes
    # =========================================================================
    ERROR_TYPE = "autogen.error.type"
    ERROR_MESSAGE = "autogen.error.message"
    IS_ERROR = "autogen.is_error"

    # =========================================================================
    # Performance Attributes
    # =========================================================================
    DURATION_MS = "autogen.duration_ms"
    TIME_TO_FIRST_TOKEN_MS = "autogen.time_to_first_token_ms"

    # =========================================================================
    # Round/Turn Attributes
    # =========================================================================
    ROUND_NUMBER = "autogen.round.number"
    ROUND_SPEAKER = "autogen.round.speaker"

    # =========================================================================
    # Streaming Attributes
    # =========================================================================
    STREAM_CHUNK_COUNT = "autogen.stream.chunk_count"
    STREAM_IS_COMPLETE = "autogen.stream.is_complete"

    # =========================================================================
    # Memory Attributes
    # =========================================================================
    MEMORY_TYPE = "autogen.memory.type"
    MEMORY_OPERATION = "autogen.memory.operation"  # add, query, clear

    # =========================================================================
    # Metadata
    # =========================================================================
    METADATA = "autogen.metadata"


# Model provider mapping
MODEL_PROVIDERS = {
    "openai": "openai",
    "gpt-": "openai",
    "o1-": "openai",
    "o3-": "openai",
    "anthropic": "anthropic",
    "claude-": "anthropic",
    "gemini": "google",
    "models/gemini": "google",
    "groq": "groq",
    "mistral": "mistral",
    "deepseek": "deepseek",
    "cohere": "cohere",
    "azure": "azure",
    "ollama": "ollama",
    "together": "together",
}


def get_model_provider(model_name: str) -> str:
    """Determine the provider from a model name.

    Args:
        model_name: Model name or identifier

    Returns:
        Provider name (e.g., "openai", "anthropic")
    """
    if not model_name:
        return "unknown"

    model_lower = model_name.lower()

    for prefix, provider in MODEL_PROVIDERS.items():
        if model_lower.startswith(prefix):
            return provider

    # Check for provider:model format
    if ":" in model_name:
        provider = model_name.split(":")[0].lower()
        return provider

    # Check for provider/model format (Azure style)
    if "/" in model_name:
        provider = model_name.split("/")[0].lower()
        if provider in MODEL_PROVIDERS.values():
            return provider

    return "unknown"


def get_agent_type_name(agent: object) -> str:
    """Get the type name of an agent.

    Args:
        agent: Agent instance

    Returns:
        Agent type name
    """
    return type(agent).__name__


def get_team_type_name(team: object) -> str:
    """Get the type name of a team.

    Args:
        team: Team instance

    Returns:
        Team type name
    """
    return type(team).__name__
