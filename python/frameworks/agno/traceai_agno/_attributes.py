"""Semantic attributes for Agno Framework instrumentation.

This module provides helper functions to extract attributes from Agno
Agent objects, tools, and teams following OpenTelemetry GenAI
semantic conventions.
"""

from typing import Any, Dict, List, Optional


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
    AGENT_INSTRUCTIONS = "agent.instructions"

    # Tool attributes
    GEN_AI_TOOL_NAME = "gen_ai.tool.name"
    GEN_AI_TOOL_DESCRIPTION = "gen_ai.tool.description"
    TOOL_PARAMETERS = "gen_ai.tool.parameters"
    TOOL_RESULT = "gen_ai.tool.result"

    # Agno-specific attributes
    AGNO_AGENT_ID = "agno.agent.id"
    AGNO_TOOL_COUNT = "agno.tool_count"
    AGNO_TEAM_NAME = "agno.team.name"
    AGNO_TEAM_MEMBERS = "agno.team.members"
    AGNO_WORKFLOW_NAME = "agno.workflow.name"
    AGNO_WORKFLOW_STEP = "agno.workflow.step"
    AGNO_SESSION_ID = "agno.session.id"
    AGNO_USER_ID = "agno.user.id"
    AGNO_DEBUG_MODE = "agno.debug_mode"
    AGNO_MARKDOWN = "agno.markdown"
    AGNO_MEMORY_ENABLED = "agno.memory.enabled"
    AGNO_KNOWLEDGE_ENABLED = "agno.knowledge.enabled"


# Model provider detection patterns
MODEL_PROVIDER_PATTERNS = [
    # Specific model patterns first
    ("openai", ["gpt-", "o1-", "o3-", "text-davinci", "text-embedding"]),
    ("anthropic", ["claude"]),
    ("google", ["gemini", "palm", "bison"]),
    ("mistral", ["mistral", "mixtral", "codestral"]),
    ("meta", ["llama"]),
    ("cohere", ["command", "embed-"]),
    ("groq", ["groq/"]),
    ("together", ["together/"]),
    ("ollama", ["ollama"]),
    ("deepseek", ["deepseek"]),
    ("fireworks", ["fireworks/"]),
    # Cloud providers
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
                      "mistral", "ollama", "groq", "together", "fireworks"]:
            return prefix

    # Check for known model patterns
    for provider, patterns in MODEL_PROVIDER_PATTERNS:
        for pattern in patterns:
            if pattern in model_lower:
                return provider

    return "unknown"


def get_agent_attributes(agent: Any) -> Dict[str, Any]:
    """Extract attributes from an Agno Agent instance.

    Args:
        agent: An Agno Agent instance.

    Returns:
        Dictionary of agent attributes.
    """
    attrs = {
        SpanAttributes.AGENT_TYPE: type(agent).__name__,
    }

    # Get agent name
    name = getattr(agent, "name", None)
    if name:
        attrs[SpanAttributes.GEN_AI_AGENT_NAME] = name

    # Get agent ID
    agent_id = getattr(agent, "agent_id", None) or getattr(agent, "id", None)
    if agent_id:
        attrs[SpanAttributes.AGNO_AGENT_ID] = str(agent_id)

    # Get agent description/instructions
    description = getattr(agent, "description", None)
    if description:
        attrs[SpanAttributes.AGENT_DESCRIPTION] = _truncate(str(description), 500)

    instructions = getattr(agent, "instructions", None)
    if instructions:
        attrs[SpanAttributes.AGENT_INSTRUCTIONS] = _truncate(str(instructions), 500)

    # Get model information
    model = getattr(agent, "model", None)
    if model:
        model_id = getattr(model, "id", None) or getattr(model, "model", None)
        if model_id:
            attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] = model_id
            attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] = get_model_provider(model_id)

    # Get tool count
    tools = getattr(agent, "tools", None) or []
    attrs[SpanAttributes.AGNO_TOOL_COUNT] = len(tools)

    # Get debug mode
    debug_mode = getattr(agent, "debug_mode", None)
    if debug_mode is not None:
        attrs[SpanAttributes.AGNO_DEBUG_MODE] = debug_mode

    # Get markdown setting
    markdown = getattr(agent, "markdown", None)
    if markdown is not None:
        attrs[SpanAttributes.AGNO_MARKDOWN] = markdown

    # Check memory/knowledge
    memory = getattr(agent, "memory", None) or getattr(agent, "add_history_to_context", None)
    attrs[SpanAttributes.AGNO_MEMORY_ENABLED] = memory is not None and memory is not False

    knowledge = getattr(agent, "knowledge", None)
    attrs[SpanAttributes.AGNO_KNOWLEDGE_ENABLED] = knowledge is not None

    return attrs


def get_tool_attributes(tool: Any) -> Dict[str, Any]:
    """Extract attributes from an Agno tool.

    Args:
        tool: An Agno tool instance.

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


def get_team_attributes(team: Any) -> Dict[str, Any]:
    """Extract attributes from an Agno Team instance.

    Args:
        team: An Agno Team instance.

    Returns:
        Dictionary of team attributes.
    """
    attrs = {}

    # Get team name
    name = getattr(team, "name", None)
    if name:
        attrs[SpanAttributes.AGNO_TEAM_NAME] = name

    # Get team members
    members = getattr(team, "agents", None) or getattr(team, "members", None) or []
    if members:
        member_names = []
        for member in members:
            member_name = getattr(member, "name", None) or type(member).__name__
            member_names.append(member_name)
        attrs[SpanAttributes.AGNO_TEAM_MEMBERS] = ", ".join(member_names)

    return attrs


def get_workflow_attributes(workflow: Any) -> Dict[str, Any]:
    """Extract attributes from an Agno Workflow instance.

    Args:
        workflow: An Agno Workflow instance.

    Returns:
        Dictionary of workflow attributes.
    """
    attrs = {}

    # Get workflow name
    name = getattr(workflow, "name", None) or getattr(workflow, "__name__", None)
    if name:
        attrs[SpanAttributes.AGNO_WORKFLOW_NAME] = name

    return attrs


def get_model_attributes(
    model: Any,
    response: Any = None,
) -> Dict[str, Any]:
    """Extract attributes from an Agno model and response.

    Args:
        model: An Agno model instance.
        response: Optional response object with usage data.

    Returns:
        Dictionary of model attributes.
    """
    attrs = {}

    # Get model ID
    model_id = getattr(model, "id", None) or getattr(model, "model", None)
    if model_id:
        attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] = model_id
        attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] = get_model_provider(model_id)

    # Get model parameters
    temperature = getattr(model, "temperature", None)
    if temperature is not None:
        attrs[SpanAttributes.GEN_AI_REQUEST_TEMPERATURE] = temperature

    max_tokens = getattr(model, "max_tokens", None)
    if max_tokens is not None:
        attrs[SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS] = max_tokens

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
    """Create a trace context dictionary for Agno agents.

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
        context[SpanAttributes.AGNO_SESSION_ID] = session_id
    if user_id:
        context[SpanAttributes.AGNO_USER_ID] = user_id
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
