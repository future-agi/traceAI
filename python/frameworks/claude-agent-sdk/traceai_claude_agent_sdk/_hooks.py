"""Hook-based tool tracing for Claude Agent SDK.

This module provides PreToolUse and PostToolUse hooks that trace all tool
executions using OpenTelemetry spans.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from ._attributes import (
    ClaudeAgentAttributes,
    ClaudeAgentSpanKind,
    BUILTIN_TOOLS,
    get_tool_source,
)

logger = logging.getLogger(__name__)

# Storage for correlating PreToolUse and PostToolUse events
# Key: tool_use_id, Value: (span, start_time, tool_name)
_active_tool_spans: Dict[str, Tuple[Span, float, str]] = {}

# Storage for subagent spans
# Key: tool_use_id (Task tool), Value: span
_subagent_spans: Dict[str, Span] = {}

# Parent span context for tool tracing
_parent_span: Optional[Span] = None

# MCP server configurations for tool attribution
_mcp_servers: Dict[str, Dict[str, Any]] = {}


def set_parent_span(span: Span) -> None:
    """Set the parent span for tool tracing."""
    global _parent_span
    _parent_span = span


def get_parent_span() -> Optional[Span]:
    """Get the current parent span."""
    return _parent_span


def clear_parent_span() -> None:
    """Clear the parent span."""
    global _parent_span
    _parent_span = None


def set_mcp_servers(servers: Dict[str, Dict[str, Any]]) -> None:
    """Set MCP server configurations for tool attribution."""
    global _mcp_servers
    _mcp_servers = servers or {}


def get_subagent_span(parent_tool_use_id: str) -> Optional[Span]:
    """Get the subagent span for a given Task tool_use_id."""
    return _subagent_spans.get(parent_tool_use_id)


def safe_json_serialize(obj: Any, max_length: int = 5000) -> str:
    """Safely serialize an object to JSON string.

    Args:
        obj: Object to serialize
        max_length: Maximum string length

    Returns:
        JSON string, truncated if necessary
    """
    try:
        if obj is None:
            return ""
        if isinstance(obj, str):
            result = obj
        elif isinstance(obj, (dict, list)):
            result = json.dumps(obj, default=str, ensure_ascii=False)
        else:
            result = str(obj)

        if len(result) > max_length:
            return result[: max_length - 3] + "..."
        return result
    except Exception:
        return str(obj)[:max_length]


def create_pre_tool_use_hook(tracer: trace_api.Tracer) -> Callable:
    """Create a PreToolUse hook for tracing tool execution.

    Args:
        tracer: OpenTelemetry tracer instance

    Returns:
        Async hook function compatible with Claude Agent SDK
    """

    async def pre_tool_use_hook(
        input_data: Dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> Dict[str, Any]:
        """Trace tool execution before it starts.

        Args:
            input_data: Contains tool_name, tool_input, session_id
            tool_use_id: Unique identifier for this tool invocation
            context: Hook context

        Returns:
            Empty dict to allow execution to proceed
        """
        if not tool_use_id:
            logger.debug("PreToolUse hook called without tool_use_id, skipping")
            return {}

        tool_name = str(input_data.get("tool_name", "unknown_tool"))
        tool_input = input_data.get("tool_input", {})

        try:
            # Determine parent span
            parent_span = get_parent_span()
            parent_tool_use_id = input_data.get("parent_tool_use_id")

            # If this tool is within a subagent, use subagent span as parent
            if parent_tool_use_id and parent_tool_use_id in _subagent_spans:
                parent_span = _subagent_spans[parent_tool_use_id]

            # Determine span kind based on tool type
            if tool_name == "Task":
                span_kind_attr = ClaudeAgentSpanKind.SUBAGENT
            else:
                span_kind_attr = ClaudeAgentSpanKind.TOOL_EXECUTION

            # Create span with parent context
            context_to_use = None
            if parent_span:
                context_to_use = trace_api.set_span_in_context(parent_span)

            span = tracer.start_span(
                name=f"tool.{tool_name}",
                kind=SpanKind.INTERNAL,
                context=context_to_use,
            )

            start_time = time.time()

            # Set span attributes
            span.set_attribute(ClaudeAgentAttributes.SPAN_KIND, span_kind_attr.value)
            span.set_attribute(ClaudeAgentAttributes.GEN_AI_TOOL_NAME, tool_name)
            span.set_attribute(ClaudeAgentAttributes.TOOL_USE_ID, tool_use_id)
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_SOURCE,
                get_tool_source(tool_name, _mcp_servers),
            )

            if tool_input:
                span.set_attribute(
                    ClaudeAgentAttributes.TOOL_INPUT,
                    safe_json_serialize(tool_input),
                )

            if parent_tool_use_id:
                span.set_attribute(
                    ClaudeAgentAttributes.PARENT_TOOL_USE_ID,
                    parent_tool_use_id,
                )

            # Add tool-specific attributes
            _set_tool_specific_attributes(span, tool_name, tool_input)

            # Store span for PostToolUse correlation
            _active_tool_spans[tool_use_id] = (span, start_time, tool_name)

            # If this is a Task tool, also store as subagent span
            if tool_name == "Task":
                _subagent_spans[tool_use_id] = span
                # Set subagent-specific attributes
                subagent_type = tool_input.get("subagent_type", "")
                description = tool_input.get("description", "")
                prompt = tool_input.get("prompt", "")

                span.set_attribute(
                    ClaudeAgentAttributes.SUBAGENT_TYPE,
                    subagent_type,
                )
                span.set_attribute(
                    ClaudeAgentAttributes.SUBAGENT_DESCRIPTION,
                    description[:500] if description else "",
                )
                span.set_attribute(
                    ClaudeAgentAttributes.SUBAGENT_PROMPT,
                    prompt[:1000] if prompt else "",
                )

            # Add event for tool start
            span.add_event(
                "tool_start",
                {
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                },
            )

            logger.debug(f"Started tool trace: {tool_name} (id={tool_use_id})")

        except Exception as e:
            logger.warning(f"Error in PreToolUse hook for {tool_name}: {e}")

        return {}

    return pre_tool_use_hook


def create_post_tool_use_hook(tracer: trace_api.Tracer) -> Callable:
    """Create a PostToolUse hook for completing tool traces.

    Args:
        tracer: OpenTelemetry tracer instance

    Returns:
        Async hook function compatible with Claude Agent SDK
    """

    async def post_tool_use_hook(
        input_data: Dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> Dict[str, Any]:
        """Complete tool trace after execution.

        Args:
            input_data: Contains tool_name, tool_input, tool_response
            tool_use_id: Unique identifier for this tool invocation
            context: Hook context

        Returns:
            Empty dict
        """
        if not tool_use_id:
            logger.debug("PostToolUse hook called without tool_use_id, skipping")
            return {}

        tool_name = str(input_data.get("tool_name", "unknown_tool"))
        tool_response = input_data.get("tool_response")

        try:
            # Get the span from PreToolUse
            span_info = _active_tool_spans.pop(tool_use_id, None)
            if not span_info:
                logger.debug(f"No matching PreToolUse for {tool_name} (id={tool_use_id})")
                return {}

            span, start_time, _ = span_info
            duration_ms = (time.time() - start_time) * 1000

            # Set output and duration
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_OUTPUT,
                safe_json_serialize(tool_response),
            )
            span.set_attribute(ClaudeAgentAttributes.TOOL_DURATION_MS, duration_ms)

            # Check for errors
            is_error = False
            error_message = None
            if isinstance(tool_response, dict):
                is_error = tool_response.get("is_error", False)
                if is_error:
                    error_message = tool_response.get("output", str(tool_response))

            span.set_attribute(ClaudeAgentAttributes.TOOL_IS_ERROR, is_error)

            if is_error:
                span.set_attribute(
                    ClaudeAgentAttributes.TOOL_ERROR_MESSAGE,
                    error_message[:500] if error_message else "",
                )
                span.set_status(Status(StatusCode.ERROR, error_message))
            else:
                span.set_status(Status(StatusCode.OK))

            # Add completion event
            span.add_event(
                "tool_complete",
                {
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "duration_ms": duration_ms,
                    "is_error": is_error,
                },
            )

            # End the span
            span.end()

            # Clean up subagent span if this was a Task tool
            if tool_name == "Task":
                _subagent_spans.pop(tool_use_id, None)

            logger.debug(
                f"Completed tool trace: {tool_name} "
                f"(id={tool_use_id}, duration={duration_ms:.2f}ms)"
            )

        except Exception as e:
            logger.warning(f"Error in PostToolUse hook for {tool_name}: {e}")

        return {}

    return post_tool_use_hook


def _set_tool_specific_attributes(
    span: Span, tool_name: str, tool_input: Dict[str, Any]
) -> None:
    """Set tool-specific attributes based on tool type.

    Args:
        span: The span to set attributes on
        tool_name: Name of the tool
        tool_input: Tool input parameters
    """
    if tool_name == "Read":
        if "file_path" in tool_input:
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_FILE_PATH,
                tool_input["file_path"],
            )

    elif tool_name == "Write":
        if "file_path" in tool_input:
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_FILE_PATH,
                tool_input["file_path"],
            )

    elif tool_name == "Edit":
        if "file_path" in tool_input:
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_FILE_PATH,
                tool_input["file_path"],
            )

    elif tool_name == "Bash":
        if "command" in tool_input:
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_COMMAND,
                tool_input["command"][:500],
            )

    elif tool_name == "Glob":
        if "pattern" in tool_input:
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_PATTERN,
                tool_input["pattern"],
            )

    elif tool_name == "Grep":
        if "pattern" in tool_input:
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_PATTERN,
                tool_input["pattern"],
            )

    elif tool_name == "WebSearch":
        if "query" in tool_input:
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_SEARCH_QUERY,
                tool_input["query"],
            )

    elif tool_name == "WebFetch":
        if "url" in tool_input:
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_URL,
                tool_input["url"],
            )


def clear_active_tool_spans() -> None:
    """Clear all active tool spans.

    Should be called when a conversation ends to avoid memory leaks.
    """
    global _active_tool_spans, _subagent_spans

    # End any orphaned spans
    for tool_use_id, (span, start_time, tool_name) in _active_tool_spans.items():
        try:
            span.set_status(
                Status(StatusCode.ERROR, "Tool span not completed (conversation ended)")
            )
            span.end()
            logger.debug(f"Cleaned up orphaned tool span: {tool_name}")
        except Exception as e:
            logger.debug(f"Failed to clean up tool span {tool_use_id}: {e}")

    for tool_use_id, span in _subagent_spans.items():
        try:
            span.set_status(
                Status(StatusCode.ERROR, "Subagent span not completed")
            )
            span.end()
        except Exception:
            pass

    _active_tool_spans.clear()
    _subagent_spans.clear()
