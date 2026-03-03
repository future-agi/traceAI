"""Semantic attributes for Pydantic AI instrumentation.

These attributes follow OpenTelemetry semantic conventions where applicable,
with Pydantic AI-specific extensions.
"""

from enum import Enum


class PydanticAISpanKind(str, Enum):
    """Span kinds for Pydantic AI operations."""

    AGENT_RUN = "agent_run"  # Root span for agent.run()
    MODEL_REQUEST = "model_request"  # LLM model call
    TOOL_CALL = "tool_call"  # Tool function execution
    RESULT_VALIDATION = "result_validation"  # Output validation
    RETRY = "retry"  # Model retry attempt
    STREAM = "stream"  # Streaming response


class PydanticAIAttributes:
    """Semantic attributes for Pydantic AI spans.

    Follows OpenTelemetry GenAI semantic conventions where applicable.
    """

    # =========================================================================
    # Span Kind
    # =========================================================================
    SPAN_KIND = "pydantic_ai.span_kind"

    # =========================================================================
    # Agent Attributes
    # =========================================================================
    GEN_AI_AGENT_NAME = "pydantic_ai.agent.name"
    AGENT_MODEL = "pydantic_ai.agent.model"
    AGENT_MODEL_PROVIDER = "pydantic_ai.agent.model_provider"
    AGENT_INSTRUCTIONS = "pydantic_ai.agent.instructions"
    AGENT_RESULT_TYPE = "pydantic_ai.agent.result_type"
    AGENT_DEPS_TYPE = "pydantic_ai.agent.deps_type"

    # =========================================================================
    # Run Attributes
    # =========================================================================
    RUN_ID = "pydantic_ai.run.id"
    RUN_METHOD = "pydantic_ai.run.method"  # run, run_sync, run_stream, iter
    RUN_PROMPT = "pydantic_ai.run.prompt"
    RUN_MESSAGE_HISTORY_LENGTH = "pydantic_ai.run.message_history_length"
    RUN_RESULT = "pydantic_ai.run.result"
    RUN_IS_STRUCTURED = "pydantic_ai.run.is_structured"

    # =========================================================================
    # Model Request Attributes
    # =========================================================================
    MODEL_NAME = "gen_ai.request.model"
    MODEL_PROVIDER = "gen_ai.system"
    MODEL_TEMPERATURE = "gen_ai.request.temperature"
    MODEL_MAX_TOKENS = "gen_ai.request.max_tokens"
    MODEL_TOP_P = "gen_ai.request.top_p"

    # =========================================================================
    # Tool Attributes
    # =========================================================================
    GEN_AI_TOOL_NAME = "pydantic_ai.tool.name"
    GEN_AI_TOOL_DESCRIPTION = "pydantic_ai.tool.description"
    TOOL_ARGS = "pydantic_ai.tool.args"
    TOOL_RESULT = "pydantic_ai.tool.result"
    TOOL_IS_ERROR = "pydantic_ai.tool.is_error"
    TOOL_ERROR_MESSAGE = "pydantic_ai.tool.error_message"
    TOOL_RETRY_COUNT = "pydantic_ai.tool.retry_count"
    TOOL_DURATION_MS = "pydantic_ai.tool.duration_ms"

    # =========================================================================
    # Message Attributes
    # =========================================================================
    MESSAGE_ROLE = "pydantic_ai.message.role"
    MESSAGE_CONTENT = "pydantic_ai.message.content"
    MESSAGE_TOOL_CALLS = "pydantic_ai.message.tool_calls"
    MESSAGE_TOOL_CALL_ID = "pydantic_ai.message.tool_call_id"

    # =========================================================================
    # Usage Attributes (GenAI Semantic Conventions)
    # =========================================================================
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
    USAGE_CACHE_READ_TOKENS = "gen_ai.usage.cache_read_tokens"
    USAGE_CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation_tokens"

    # Usage Limits
    USAGE_LIMIT_REQUEST_TOKENS = "pydantic_ai.usage_limit.request_tokens"
    USAGE_LIMIT_RESPONSE_TOKENS = "pydantic_ai.usage_limit.response_tokens"
    USAGE_LIMIT_TOTAL_TOKENS = "pydantic_ai.usage_limit.total_tokens"
    USAGE_LIMIT_REQUEST_COUNT = "pydantic_ai.usage_limit.request_count"
    USAGE_LIMIT_TOOL_CALLS = "pydantic_ai.usage_limit.tool_calls"

    # =========================================================================
    # Cost Attributes
    # =========================================================================
    COST_TOTAL_USD = "pydantic_ai.cost.total_usd"
    COST_INPUT_USD = "pydantic_ai.cost.input_usd"
    COST_OUTPUT_USD = "pydantic_ai.cost.output_usd"

    # =========================================================================
    # Performance Attributes
    # =========================================================================
    DURATION_MS = "pydantic_ai.duration_ms"
    TIME_TO_FIRST_TOKEN_MS = "pydantic_ai.time_to_first_token_ms"

    # =========================================================================
    # Retry Attributes
    # =========================================================================
    RETRY_COUNT = "pydantic_ai.retry.count"
    RETRY_REASON = "pydantic_ai.retry.reason"
    RETRY_MAX_RETRIES = "pydantic_ai.retry.max_retries"

    # =========================================================================
    # Error Attributes
    # =========================================================================
    ERROR_TYPE = "pydantic_ai.error.type"
    ERROR_MESSAGE = "pydantic_ai.error.message"
    IS_ERROR = "pydantic_ai.is_error"

    # =========================================================================
    # Streaming Attributes
    # =========================================================================
    STREAM_CHUNK_COUNT = "pydantic_ai.stream.chunk_count"
    STREAM_IS_STRUCTURED = "pydantic_ai.stream.is_structured"

    # =========================================================================
    # Result Validation Attributes
    # =========================================================================
    VALIDATION_IS_VALID = "pydantic_ai.validation.is_valid"
    VALIDATION_ERROR = "pydantic_ai.validation.error"
    VALIDATION_RETRIES = "pydantic_ai.validation.retries"

    # =========================================================================
    # Metadata Attributes
    # =========================================================================
    METADATA = "pydantic_ai.metadata"
    METADATA_TAGS = "pydantic_ai.metadata.tags"


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
    "bedrock": "aws",
    "vertex": "google",
    "ollama": "ollama",
    "together": "together",
    "fireworks": "fireworks",
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

    return "unknown"
