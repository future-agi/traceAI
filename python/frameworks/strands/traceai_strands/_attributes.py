"""Semantic attributes for Strands Agents instrumentation.

This module provides helper functions to extract attributes from Strands
Agent objects, tools, and model providers following OpenTelemetry GenAI
semantic conventions.
"""

from typing import Any, Dict, List, Optional


# GenAI Semantic Convention attribute names
class SpanAttributes:
    """OpenTelemetry GenAI semantic convention attributes."""

    # LLM attributes
    GEN_AI_PROVIDER_NAME = "gen_ai.system"
    GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
    LLM_RESPONSE_MODEL = "gen_ai.response.model"
    GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
    GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
    GEN_AI_REQUEST_TOP_P = "gen_ai.request.top_p"
    GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    GEN_AI_USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"

    # Agent attributes
    GEN_AI_AGENT_NAME = "agent.name"
    AGENT_TYPE = "agent.type"
    AGENT_DESCRIPTION = "agent.description"

    # Tool attributes
    GEN_AI_TOOL_NAME = "gen_ai.tool.name"
    GEN_AI_TOOL_DESCRIPTION = "gen_ai.tool.description"
    TOOL_PARAMETERS = "gen_ai.tool.parameters"
    TOOL_RESULT = "gen_ai.tool.result"

    # Strands-specific attributes
    STRANDS_MODEL_PROVIDER = "strands.model.provider"
    STRANDS_SYSTEM_PROMPT = "strands.system_prompt"
    STRANDS_TOOL_COUNT = "strands.tool_count"
    STRANDS_MCP_SERVER = "strands.mcp.server"
    STRANDS_SESSION_ID = "strands.session.id"
    STRANDS_USER_ID = "strands.user.id"
    STRANDS_CACHE_READ_TOKENS = "strands.cache.read_tokens"
    STRANDS_CACHE_WRITE_TOKENS = "strands.cache.write_tokens"


# Model provider detection patterns - ordered by specificity
# Note: Check specific providers before bedrock (which is a multi-provider platform)
MODEL_PROVIDER_PATTERNS = [
    # Explicit provider prefixes are checked first (see below)
    # Then check specific model patterns
    ("openai", ["gpt-", "o1-", "o3-", "text-davinci", "text-embedding"]),
    ("anthropic", ["claude"]),
    ("google", ["gemini", "palm", "bison"]),
    ("mistral", ["mistral", "mixtral", "codestral"]),
    ("meta", ["llama"]),
    ("cohere", ["command", "embed-"]),
    ("ollama", ["ollama"]),
    # Bedrock-specific patterns (when not matched by above)
    ("bedrock", ["amazon.", "ai21.", "stability."]),
]


def get_model_provider(model_name: Optional[str]) -> str:
    """Detect the model provider from a model name.

    Args:
        model_name: The model name or identifier.

    Returns:
        The detected provider name or "unknown".
    """
    if not model_name:
        return "unknown"

    model_lower = model_name.lower()

    # Check for explicit provider prefixes (e.g., "ollama/llama3", "azure/gpt-4")
    if "/" in model_name:
        prefix = model_name.split("/")[0].lower()
        if prefix in ["bedrock", "azure", "openai", "anthropic", "google", "mistral", "ollama"]:
            return prefix

    # Check for Bedrock ARN patterns (e.g., "us.anthropic.claude-sonnet-4")
    if model_lower.startswith("us.") or model_lower.startswith("eu."):
        return "bedrock"

    # Check for known model patterns in order of specificity
    for provider, patterns in MODEL_PROVIDER_PATTERNS:
        for pattern in patterns:
            if pattern in model_lower:
                return provider

    return "unknown"


def get_agent_attributes(agent: Any) -> Dict[str, Any]:
    """Extract attributes from a Strands Agent instance.

    Args:
        agent: A Strands Agent instance.

    Returns:
        Dictionary of agent attributes.
    """
    attrs = {
        SpanAttributes.AGENT_TYPE: type(agent).__name__,
    }

    # Get system prompt
    system_prompt = getattr(agent, "system_prompt", None)
    if system_prompt:
        # Truncate long prompts
        if len(system_prompt) > 500:
            system_prompt = system_prompt[:497] + "..."
        attrs[SpanAttributes.STRANDS_SYSTEM_PROMPT] = system_prompt

    # Get model information
    model = getattr(agent, "model", None)
    if model:
        model_name = getattr(model, "model_id", None) or getattr(model, "model", None)
        if model_name:
            attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] = model_name
            attrs[SpanAttributes.STRANDS_MODEL_PROVIDER] = get_model_provider(model_name)
            attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] = get_model_provider(model_name)

    # Get tool count
    tools = getattr(agent, "tools", None) or []
    attrs[SpanAttributes.STRANDS_TOOL_COUNT] = len(tools)

    # Get trace attributes if present
    trace_attrs = getattr(agent, "trace_attributes", None) or {}
    if "session.id" in trace_attrs:
        attrs[SpanAttributes.STRANDS_SESSION_ID] = trace_attrs["session.id"]
    if "user.id" in trace_attrs:
        attrs[SpanAttributes.STRANDS_USER_ID] = trace_attrs["user.id"]

    return attrs


def get_tool_attributes(tool: Any) -> Dict[str, Any]:
    """Extract attributes from a Strands tool.

    Args:
        tool: A Strands tool (function decorated with @tool or MCP tool).

    Returns:
        Dictionary of tool attributes.
    """
    attrs = {}

    # Get tool name
    name = getattr(tool, "__name__", None) or getattr(tool, "name", None)
    if name:
        attrs[SpanAttributes.GEN_AI_TOOL_NAME] = name

    # Get tool description from docstring
    description = getattr(tool, "__doc__", None) or getattr(tool, "description", None)
    if description:
        # Truncate long descriptions
        if len(description) > 500:
            description = description[:497] + "..."
        attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION] = description

    return attrs


def get_model_attributes(model: Any, response: Any = None) -> Dict[str, Any]:
    """Extract attributes from a Strands model provider and response.

    Args:
        model: A Strands model provider instance.
        response: Optional response object with usage data.

    Returns:
        Dictionary of model attributes.
    """
    attrs = {}

    # Get model name
    model_name = getattr(model, "model_id", None) or getattr(model, "model", None)
    if model_name:
        attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] = model_name
        attrs[SpanAttributes.STRANDS_MODEL_PROVIDER] = get_model_provider(model_name)
        attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] = get_model_provider(model_name)

    # Get model parameters
    if hasattr(model, "temperature"):
        attrs[SpanAttributes.GEN_AI_REQUEST_TEMPERATURE] = model.temperature
    if hasattr(model, "max_tokens"):
        attrs[SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS] = model.max_tokens
    if hasattr(model, "top_p"):
        attrs[SpanAttributes.GEN_AI_REQUEST_TOP_P] = model.top_p

    # Extract usage from response
    if response:
        usage = getattr(response, "usage", None)
        if usage:
            input_tokens = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", None)

            if input_tokens is not None:
                attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = input_tokens
            if output_tokens is not None:
                attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = output_tokens
            if input_tokens is not None and output_tokens is not None:
                attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = input_tokens + output_tokens

            # Cache metrics (Bedrock-specific)
            cache_read = getattr(usage, "cache_read_input_tokens", None)
            cache_write = getattr(usage, "cache_creation_input_tokens", None)
            if cache_read is not None:
                attrs[SpanAttributes.STRANDS_CACHE_READ_TOKENS] = cache_read
            if cache_write is not None:
                attrs[SpanAttributes.STRANDS_CACHE_WRITE_TOKENS] = cache_write

    return attrs


def get_trace_attributes_from_config(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create trace attributes dict for Strands Agent configuration.

    This creates the trace_attributes dict that can be passed to Agent().

    Args:
        session_id: Optional session identifier.
        user_id: Optional user identifier.
        tags: Optional list of tags.
        metadata: Optional additional metadata.

    Returns:
        Dictionary suitable for trace_attributes parameter.
    """
    attrs = {}

    if session_id:
        attrs["session.id"] = session_id
    if user_id:
        attrs["user.id"] = user_id
    if tags:
        attrs["tags"] = tags
    if metadata:
        attrs.update(metadata)

    return attrs
