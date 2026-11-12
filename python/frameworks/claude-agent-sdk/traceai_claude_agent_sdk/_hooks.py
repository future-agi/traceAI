"""Hook-based tool tracing for Claude Agent SDK.

This module provides PreToolUse and PostToolUse hooks that trace all tool
executions using OpenTelemetry spans.
"""

import json
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from ._attachments import media_safe
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

_MAX_DEFERRED_ENDS = 256
#: A deferred tool span ends at most this long after its hook, even if its result never arrives.
DEFERRED_END_TIMEOUT_S = 30.0


class ToolDeferrals:
    """One client's tool spans whose end waits for their result (tool-result media capture on).

    The SDK streams a tool result AFTER the PostToolUse hook, so while that client's
    ``receive_response()`` conversation span is live, a completed tool span stays recording
    until its ToolResultBlock has been captured (its media refs are projected on it), then
    ends at the time the hook recorded. Outside a live conversation (``receive_messages()``,
    or a hook that fires before ``receive_response()`` starts) the hook ends the span as
    before. Each client has its own tables, so one conversation's end flushes only its own.

    ``ends``: tool_use_id -> (span, end_time_ns, timer handle or None), bounded and
    time-limited. ``results_seen``: results streamed while their span was still open
    (result-first), so PostToolUse ends those now. ``live``: open conversation spans.
    """

    __slots__ = ("ends", "results_seen", "live")

    def __init__(self) -> None:
        self.ends: "OrderedDict[str, Tuple[Span, int, Any]]" = OrderedDict()
        self.results_seen: "OrderedDict[str, bool]" = OrderedDict()
        self.live = 0

# Tools that launch a subagent: "Task" in older Claude Code releases, "Agent" in current ones
SUBAGENT_TOOLS = ("Task", "Agent")

# Storage for subagent spans
# Key: tool_use_id (subagent launch), Value: span
_subagent_spans: Dict[str, Span] = {}

# Current SDKs attribute a subagent's tool hooks by agent_id, announced in SubagentStart in launch
# order: launches waiting for their agent, and each agent_id's launch.
_pending_launches: List[Tuple[str, str]] = []
_agent_launches: Dict[str, str] = {}

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


def _launch_for_agent(agent_id: Optional[str], agent_type: str = "") -> Optional[str]:
    """The launch a subagent belongs to: bound once, to the oldest pending launch of its type."""
    if not agent_id:
        return None
    if agent_id in _agent_launches:
        return _agent_launches[agent_id]
    typed = [launch for launch, kind in _pending_launches if kind == agent_type]
    chosen = (typed or [launch for launch, _ in _pending_launches] or [None])[0]
    if chosen is None:
        return None
    _pending_launches[:] = [item for item in _pending_launches if item[0] != chosen]
    _agent_launches[agent_id] = chosen
    return chosen


def create_subagent_start_hook(tracer: trace_api.Tracer) -> Callable:
    """Create a SubagentStart hook that binds a starting subagent to its launch."""

    async def subagent_start_hook(
        input_data: Dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> Dict[str, Any]:
        try:
            _launch_for_agent(input_data.get("agent_id"), str(input_data.get("agent_type") or ""))
        except Exception as e:
            logger.warning(f"Error in SubagentStart hook: {e}")
        return {}

    return subagent_start_hook


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
            parent_tool_use_id = input_data.get("parent_tool_use_id") or _launch_for_agent(
                input_data.get("agent_id"), str(input_data.get("agent_type") or "")
            )

            # If this tool is within a subagent, use subagent span as parent
            if parent_tool_use_id and parent_tool_use_id in _subagent_spans:
                parent_span = _subagent_spans[parent_tool_use_id]

            # Determine span kind based on tool type
            if tool_name in SUBAGENT_TOOLS:
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

            # If this launches a subagent, also store as subagent span
            if tool_name in SUBAGENT_TOOLS:
                _subagent_spans[tool_use_id] = span
                _pending_launches.append((tool_use_id, str(tool_input.get("subagent_type") or "")))
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


def _end_deferred(entry: Tuple[Span, int, Any]) -> None:
    span, end_ns, handle = entry
    try:
        if handle is not None:
            handle.cancel()
    except Exception:
        pass
    try:
        span.end(end_time=end_ns)
    except Exception as e:
        logger.debug(f"Failed to end deferred tool span: {e}")


def _expire_deferred(deferrals: ToolDeferrals, tool_use_id: str, entry: Tuple[Span, int, Any]) -> None:
    if deferrals.ends.get(tool_use_id) is entry:
        del deferrals.ends[tool_use_id]
        _end_deferred(entry)


def _defer_tool_end(deferrals: ToolDeferrals, tool_use_id: str, span: Span, end_ns: int) -> bool:
    """Keep a completed tool span recording until its result is captured. False: end it now."""
    if deferrals.results_seen.pop(tool_use_id, None):
        return False  # Its result was already captured on the open span.
    if deferrals.live <= 0:
        return False  # No receive_response() conversation will capture its result: end now.
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except Exception:
        return False  # No asyncio loop to bound the wait (another async backend): end now.
    try:
        previous = deferrals.ends.pop(tool_use_id, None)
        if previous is not None:
            _end_deferred(previous)
        holder: List[Any] = []
        handle = loop.call_later(DEFERRED_END_TIMEOUT_S,
                                 lambda: _expire_deferred(deferrals, tool_use_id, holder[0]))
        entry = (span, end_ns, handle)
        holder.append(entry)
        deferrals.ends[tool_use_id] = entry
        while len(deferrals.ends) > _MAX_DEFERRED_ENDS:
            _, oldest = deferrals.ends.popitem(last=False)
            _end_deferred(oldest)
        return True
    except Exception:
        return False


def end_deferred_tool(deferrals: Optional[ToolDeferrals], tool_use_id: Any) -> None:
    """End one of this client's deferred tool spans (its result was processed), at its hook's time."""
    if deferrals is None or not isinstance(tool_use_id, str):
        return
    entry = deferrals.ends.pop(tool_use_id, None)
    if entry is not None:
        _end_deferred(entry)
    elif tool_use_id in _active_tool_spans:
        deferrals.results_seen[tool_use_id] = True
        while len(deferrals.results_seen) > _MAX_DEFERRED_ENDS:
            deferrals.results_seen.popitem(last=False)


def end_deferred_tool_spans(deferrals: Optional[ToolDeferrals]) -> None:
    """End every deferred tool span of ONE client at its recorded time (its conversation ended)."""
    if deferrals is None:
        return
    deferrals.results_seen.clear()
    while deferrals.ends:
        _, entry = deferrals.ends.popitem(last=False)
        _end_deferred(entry)


def create_post_tool_use_hook(tracer: trace_api.Tracer, deferrals: Optional[ToolDeferrals] = None) -> Callable:
    """Create a PostToolUse hook for completing tool traces.

    Args:
        tracer: OpenTelemetry tracer instance
        deferrals: the client's deferral tables when tool-result media capture is on: while
            its receive_response() conversation is live, the completed span stays recording
            until its tool result is captured (it keeps the hook's end time)

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
            end_ns = time.time_ns()
            duration_ms = (time.time() - start_time) * 1000

            # Set output and duration. Inline media bytes never go through text serialization
            # or truncation; the attachment path uploads them from the typed tool result.
            span.set_attribute(
                ClaudeAgentAttributes.TOOL_OUTPUT,
                safe_json_serialize(media_safe(tool_response)),
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

            # End the span. With media capture on and a live conversation, the tool result
            # (streamed after this hook) is captured on the still-recording span first; it
            # ends at end_ns.
            if not (deferrals is not None and _defer_tool_end(deferrals, tool_use_id, span, end_ns)):
                span.end()

            # Clean up subagent span if this launched a subagent
            if tool_name in SUBAGENT_TOOLS:
                _subagent_spans.pop(tool_use_id, None)
                _pending_launches[:] = [item for item in _pending_launches if item[0] != tool_use_id]
                for agent_id in [a for a, launch in _agent_launches.items() if launch == tool_use_id]:
                    del _agent_launches[agent_id]

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

    # Deferred tool spans belong to one client; its conversation flushes its own
    # (end_deferred_tool_spans) before calling this.

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
    _pending_launches.clear()
    _agent_launches.clear()
