"""Semantic attributes for BeeAI Framework instrumentation.

This module provides helper functions to extract attributes from BeeAI
Agent objects, tools, and workflows following OpenTelemetry GenAI
semantic conventions.
"""

from typing import Any, Dict, List, Optional, Union


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
    AGENT_ROLE = "agent.role"
    AGENT_INSTRUCTIONS = "agent.instructions"

    # Tool attributes
    GEN_AI_TOOL_NAME = "gen_ai.tool.name"
    GEN_AI_TOOL_DESCRIPTION = "gen_ai.tool.description"
    TOOL_PARAMETERS = "gen_ai.tool.parameters"
    TOOL_RESULT = "gen_ai.tool.result"

    # BeeAI-specific attributes
    BEEAI_AGENT_TYPE = "beeai.agent.type"
    BEEAI_TOOL_COUNT = "beeai.tool_count"
    BEEAI_REQUIREMENTS = "beeai.requirements"
    BEEAI_WORKFLOW_NAME = "beeai.workflow.name"
    BEEAI_WORKFLOW_STEP = "beeai.workflow.step"
    BEEAI_MEMORY_TYPE = "beeai.memory.type"
    BEEAI_SESSION_ID = "beeai.session.id"
    BEEAI_USER_ID = "beeai.user.id"


# Model provider detection patterns
MODEL_PROVIDER_PATTERNS = [
    # Specific model patterns first
    ("openai", ["gpt-", "o1-", "o3-", "text-davinci", "text-embedding"]),
    ("anthropic", ["claude"]),
    ("google", ["gemini", "palm", "bison"]),
    ("mistral", ["mistral", "mixtral", "codestral"]),
    ("meta", ["llama"]),
    ("ibm", ["granite", "ibm/"]),
    ("cohere", ["command", "embed-"]),
    ("ollama", ["ollama"]),
    ("groq", ["groq/"]),
    ("together", ["together/"]),
    # AWS/Azure last
    ("bedrock", ["amazon.", "ai21.", "stability."]),
    ("azure", ["azure/"]),
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

    # Check for explicit provider prefixes
    if "/" in model_name:
        prefix = model_name.split("/")[0].lower()
        if prefix in ["bedrock", "azure", "openai", "anthropic", "google",
                      "mistral", "ollama", "groq", "together", "ibm"]:
            return prefix

    # Check for known model patterns
    for provider, patterns in MODEL_PROVIDER_PATTERNS:
        for pattern in patterns:
            if pattern in model_lower:
                return provider

    return "unknown"


def get_agent_attributes(agent: Any) -> Dict[str, Any]:
    """Extract attributes from a BeeAI Agent instance.

    Args:
        agent: A BeeAI Agent instance.

    Returns:
        Dictionary of agent attributes.
    """
    attrs = {
        SpanAttributes.AGENT_TYPE: type(agent).__name__,
        SpanAttributes.BEEAI_AGENT_TYPE: type(agent).__name__,
    }

    # Get agent name
    name = getattr(agent, "name", None)
    if name:
        attrs[SpanAttributes.GEN_AI_AGENT_NAME] = name

    # Get agent role
    role = getattr(agent, "role", None)
    if role:
        attrs[SpanAttributes.AGENT_ROLE] = _truncate(str(role), 500)

    # Get agent instructions
    instructions = getattr(agent, "instructions", None)
    if instructions:
        attrs[SpanAttributes.AGENT_INSTRUCTIONS] = _truncate(str(instructions), 500)

    # Get model information
    llm = getattr(agent, "llm", None)
    if llm:
        model_name = getattr(llm, "model", None) or getattr(llm, "model_id", None)
        if model_name:
            attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] = model_name
            attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] = get_model_provider(model_name)

    # Get tool count
    tools = getattr(agent, "tools", None) or []
    attrs[SpanAttributes.BEEAI_TOOL_COUNT] = len(tools)

    # Get requirements
    requirements = getattr(agent, "requirements", None)
    if requirements:
        req_names = []
        if isinstance(requirements, list):
            for req in requirements:
                req_name = getattr(req, "name", None) or type(req).__name__
                req_names.append(req_name)
        attrs[SpanAttributes.BEEAI_REQUIREMENTS] = ", ".join(req_names)

    # Get memory type
    memory = getattr(agent, "memory", None)
    if memory:
        attrs[SpanAttributes.BEEAI_MEMORY_TYPE] = type(memory).__name__

    return attrs


def get_tool_attributes(tool: Any) -> Dict[str, Any]:
    """Extract attributes from a BeeAI tool.

    Args:
        tool: A BeeAI tool instance.

    Returns:
        Dictionary of tool attributes.
    """
    attrs = {}

    # Get tool name
    name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
    if name:
        attrs[SpanAttributes.GEN_AI_TOOL_NAME] = name

    # Get tool description
    description = getattr(tool, "description", None) or getattr(tool, "__doc__", None)
    if description:
        attrs[SpanAttributes.GEN_AI_TOOL_DESCRIPTION] = _truncate(str(description), 500)

    return attrs


def get_workflow_attributes(workflow: Any) -> Dict[str, Any]:
    """Extract attributes from a BeeAI workflow.

    Args:
        workflow: A BeeAI workflow instance.

    Returns:
        Dictionary of workflow attributes.
    """
    attrs = {}

    # Get workflow name
    name = getattr(workflow, "name", None) or getattr(workflow, "__name__", None)
    if name:
        attrs[SpanAttributes.BEEAI_WORKFLOW_NAME] = name

    return attrs


def get_model_attributes(
    model: Any,
    response: Any = None,
) -> Dict[str, Any]:
    """Extract attributes from a BeeAI model and response.

    Args:
        model: A BeeAI model/LLM instance.
        response: Optional response object with usage data.

    Returns:
        Dictionary of model attributes.
    """
    attrs = {}

    # Get model name
    model_name = getattr(model, "model", None) or getattr(model, "model_id", None)
    if model_name:
        attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] = model_name
        attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] = get_model_provider(model_name)

    # Get model parameters
    if hasattr(model, "temperature"):
        temp = getattr(model, "temperature", None)
        if temp is not None:
            attrs[SpanAttributes.GEN_AI_REQUEST_TEMPERATURE] = temp

    if hasattr(model, "max_tokens"):
        max_tok = getattr(model, "max_tokens", None)
        if max_tok is not None:
            attrs[SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS] = max_tok

    if hasattr(model, "top_p"):
        top_p = getattr(model, "top_p", None)
        if top_p is not None:
            attrs[SpanAttributes.GEN_AI_REQUEST_TOP_P] = top_p

    # Extract usage from response
    if response:
        usage = getattr(response, "usage", None)
        if usage:
            input_tokens = (
                getattr(usage, "input_tokens", None) or
                getattr(usage, "prompt_tokens", None)
            )
            output_tokens = (
                getattr(usage, "output_tokens", None) or
                getattr(usage, "completion_tokens", None)
            )

            if input_tokens is not None:
                attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = input_tokens
            if output_tokens is not None:
                attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = output_tokens
            if input_tokens is not None and output_tokens is not None:
                attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = input_tokens + output_tokens

    return attrs


def create_trace_context(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a trace context dictionary for BeeAI agents.

    Args:
        session_id: Optional session identifier.
        user_id: Optional user identifier.
        tags: Optional list of tags.
        metadata: Optional additional metadata.

    Returns:
        Dictionary of trace context attributes.
    """
    context = {}

    if session_id:
        context[SpanAttributes.BEEAI_SESSION_ID] = session_id
    if user_id:
        context[SpanAttributes.BEEAI_USER_ID] = user_id
    if tags:
        context["tags"] = tags
    if metadata:
        context.update(metadata)

    return context


def _truncate(text: str, max_length: int = 500) -> str:
    """Truncate text to a maximum length.

    Args:
        text: Text to truncate.
        max_length: Maximum length.

    Returns:
        Truncated text with ellipsis if needed.
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."
