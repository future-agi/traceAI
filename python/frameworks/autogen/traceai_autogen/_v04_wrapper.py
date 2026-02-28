"""AutoGen v0.4 AgentChat wrapper for instrumentation.

This module provides wrapping for AutoGen v0.4's new architecture including:
- AssistantAgent and BaseChatAgent
- Teams (RoundRobinGroupChat, SelectorGroupChat, etc.)
- Tool execution
- Message handling
"""

import functools
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, TypeVar

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Span names
AGENT_RUN_SPAN_NAME = "autogen.agent.run"
AGENT_ON_MESSAGES_SPAN_NAME = "autogen.agent.on_messages"
TEAM_RUN_SPAN_NAME = "autogen.team.run"
TEAM_RUN_STREAM_SPAN_NAME = "autogen.team.run_stream"
TOOL_CALL_SPAN_NAME = "autogen.tool.call"


def safe_serialize(obj: Any, max_length: int = 2000) -> str:
    """Safely serialize an object to string.

    Args:
        obj: Object to serialize
        max_length: Maximum string length

    Returns:
        Serialized string
    """
    if obj is None:
        return ""

    if isinstance(obj, str):
        result = obj
    elif isinstance(obj, (dict, list)):
        try:
            result = json.dumps(obj, default=str)
        except (TypeError, ValueError):
            result = str(obj)
    else:
        result = str(obj)

    if len(result) > max_length:
        return result[: max_length - 3] + "..."

    return result


def extract_agent_info(agent: Any) -> Dict[str, Any]:
    """Extract information from an AutoGen v0.4 agent.

    Args:
        agent: BaseChatAgent instance

    Returns:
        Dict with agent info
    """
    info = {
        "agent_name": getattr(agent, "name", "unknown"),
        "agent_type": type(agent).__name__,
    }

    # Get description if available
    if hasattr(agent, "description"):
        info["agent_description"] = str(agent.description)[:500] if agent.description else None

    # Check for model client
    if hasattr(agent, "_model_client"):
        model_client = agent._model_client
        if model_client:
            # Try to get model name
            if hasattr(model_client, "model"):
                info["model_name"] = model_client.model
            elif hasattr(model_client, "_model"):
                info["model_name"] = model_client._model

    # Count tools
    if hasattr(agent, "_tools"):
        info["tool_count"] = len(agent._tools) if agent._tools else 0

    # Check for memory
    if hasattr(agent, "_memory"):
        info["has_memory"] = bool(agent._memory)

    return info


def extract_team_info(team: Any) -> Dict[str, Any]:
    """Extract information from an AutoGen v0.4 team.

    Args:
        team: Team instance (RoundRobinGroupChat, etc.)

    Returns:
        Dict with team info
    """
    info = {
        "team_type": type(team).__name__,
    }

    # Get participants
    if hasattr(team, "_participants"):
        participants = team._participants
        if participants:
            info["participant_count"] = len(participants)
            info["participants"] = [getattr(p, "name", str(p)) for p in participants]

    # Get termination condition
    if hasattr(team, "_termination_condition"):
        term = team._termination_condition
        if term:
            info["termination_condition"] = type(term).__name__

    # Get max turns
    if hasattr(team, "_max_turns"):
        info["max_turns"] = team._max_turns

    return info


def extract_message_info(message: Any) -> Dict[str, Any]:
    """Extract information from an AutoGen message.

    Args:
        message: Message object

    Returns:
        Dict with message info
    """
    info = {
        "message_type": type(message).__name__,
    }

    # Get content
    if hasattr(message, "content"):
        info["content"] = safe_serialize(message.content, 500)

    # Get source
    if hasattr(message, "source"):
        info["source"] = str(message.source)

    # Get models usage
    if hasattr(message, "models_usage"):
        usage = message.models_usage
        if usage:
            info["models_usage"] = safe_serialize(usage)

    return info


def extract_usage_from_result(result: Any) -> Optional[Dict[str, Any]]:
    """Extract token usage from a task result.

    Args:
        result: TaskResult or Response object

    Returns:
        Dict with usage info or None
    """
    if not result:
        return None

    # Check for messages with usage
    messages = getattr(result, "messages", None)
    if not messages:
        return None

    total_input = 0
    total_output = 0

    for msg in messages:
        usage = getattr(msg, "models_usage", None)
        if usage:
            if isinstance(usage, dict):
                for model_usage in usage.values():
                    if hasattr(model_usage, "prompt_tokens"):
                        total_input += model_usage.prompt_tokens or 0
                    if hasattr(model_usage, "completion_tokens"):
                        total_output += model_usage.completion_tokens or 0
            elif hasattr(usage, "prompt_tokens"):
                total_input += usage.prompt_tokens or 0
                total_output += usage.completion_tokens or 0

    if total_input or total_output:
        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
        }

    return None


def wrap_agent_on_messages(
    original_method: Callable,
    tracer: trace_api.Tracer,
) -> Callable:
    """Wrap an agent's on_messages method with tracing.

    Args:
        original_method: Original on_messages method
        tracer: OpenTelemetry tracer

    Returns:
        Wrapped method
    """

    @functools.wraps(original_method)
    async def async_wrapper(self, messages, cancellation_token=None):
        run_id = str(uuid.uuid4())
        agent_info = extract_agent_info(self)

        attributes = {
            "autogen.span_kind": "agent_run",
            "autogen.run.id": run_id,
            "autogen.agent.name": agent_info.get("agent_name", "unknown"),
            "autogen.agent.type": agent_info.get("agent_type", "unknown"),
        }

        if agent_info.get("model_name"):
            attributes["gen_ai.request.model"] = agent_info["model_name"]

        if agent_info.get("tool_count"):
            attributes["autogen.agent.tool_count"] = agent_info["tool_count"]

        # Record input messages
        if messages:
            attributes["autogen.input.message_count"] = len(messages)
            if len(messages) > 0:
                first_msg = messages[-1]  # Most recent message
                msg_info = extract_message_info(first_msg)
                attributes["autogen.input.last_message"] = msg_info.get("content", "")
                attributes["autogen.input.last_source"] = msg_info.get("source", "")

        start_time = time.time()

        with tracer.start_as_current_span(
            f"{AGENT_ON_MESSAGES_SPAN_NAME}.{agent_info.get('agent_name', 'agent')}",
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        ) as span:
            try:
                result = await original_method(self, messages, cancellation_token)

                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute("autogen.duration_ms", duration_ms)

                # Record result
                if result:
                    if hasattr(result, "chat_message"):
                        msg = result.chat_message
                        msg_info = extract_message_info(msg)
                        span.set_attribute("autogen.output.message_type", msg_info.get("message_type", ""))
                        span.set_attribute("autogen.output.content", msg_info.get("content", ""))

                    if hasattr(result, "inner_messages"):
                        inner = result.inner_messages
                        if inner:
                            span.set_attribute("autogen.output.inner_message_count", len(inner))

                span.set_attribute("autogen.is_error", False)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute("autogen.duration_ms", duration_ms)
                span.set_attribute("autogen.is_error", True)
                span.set_attribute("autogen.error.type", type(e).__name__)
                span.set_attribute("autogen.error.message", str(e))
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    return async_wrapper


def wrap_team_run(
    original_method: Callable,
    tracer: trace_api.Tracer,
    method_name: str = "run",
) -> Callable:
    """Wrap a team's run method with tracing.

    Args:
        original_method: Original run method
        tracer: OpenTelemetry tracer
        method_name: Method name (run or run_stream)

    Returns:
        Wrapped method
    """

    @functools.wraps(original_method)
    async def async_wrapper(self, task, *args, **kwargs):
        run_id = str(uuid.uuid4())
        team_info = extract_team_info(self)

        attributes = {
            "autogen.span_kind": "team_run",
            "autogen.run.id": run_id,
            "autogen.team.type": team_info.get("team_type", "unknown"),
            "autogen.run.method": method_name,
        }

        if team_info.get("participant_count"):
            attributes["autogen.team.participant_count"] = team_info["participant_count"]

        if team_info.get("participants"):
            attributes["autogen.team.participants"] = json.dumps(team_info["participants"])

        if team_info.get("termination_condition"):
            attributes["autogen.team.termination_condition"] = team_info["termination_condition"]

        if team_info.get("max_turns"):
            attributes["autogen.team.max_turns"] = team_info["max_turns"]

        # Record task
        if task:
            attributes["autogen.task.content"] = safe_serialize(task, 1000)

        start_time = time.time()

        span_name = TEAM_RUN_SPAN_NAME if method_name == "run" else TEAM_RUN_STREAM_SPAN_NAME

        with tracer.start_as_current_span(
            f"{span_name}.{team_info.get('team_type', 'team')}",
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = await original_method(self, task, *args, **kwargs)

                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute("autogen.duration_ms", duration_ms)

                # Record result
                if result:
                    if hasattr(result, "messages"):
                        span.set_attribute("autogen.task.message_count", len(result.messages))

                    if hasattr(result, "stop_reason"):
                        span.set_attribute("autogen.task.stop_reason", str(result.stop_reason))

                    # Extract usage
                    usage = extract_usage_from_result(result)
                    if usage:
                        span.set_attribute("gen_ai.usage.input_tokens", usage.get("input_tokens", 0))
                        span.set_attribute("gen_ai.usage.output_tokens", usage.get("output_tokens", 0))
                        span.set_attribute("gen_ai.usage.total_tokens", usage.get("total_tokens", 0))

                span.set_attribute("autogen.is_error", False)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute("autogen.duration_ms", duration_ms)
                span.set_attribute("autogen.is_error", True)
                span.set_attribute("autogen.error.type", type(e).__name__)
                span.set_attribute("autogen.error.message", str(e))
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    return async_wrapper


def wrap_tool_execution(
    tool_func: Callable,
    tracer: trace_api.Tracer,
    tool_name: str,
    tool_description: Optional[str] = None,
) -> Callable:
    """Wrap a tool function with tracing.

    Args:
        tool_func: Original tool function
        tracer: OpenTelemetry tracer
        tool_name: Name of the tool
        tool_description: Optional tool description

    Returns:
        Wrapped function
    """
    import asyncio

    @functools.wraps(tool_func)
    async def async_wrapper(*args, **kwargs):
        attributes = {
            "autogen.span_kind": "tool_call",
            "autogen.tool.name": tool_name,
        }

        if tool_description:
            attributes["autogen.tool.description"] = tool_description[:500]

        # Serialize args
        if kwargs:
            attributes["autogen.tool.args"] = safe_serialize(kwargs)
        elif len(args) > 0:
            attributes["autogen.tool.args"] = safe_serialize(args)

        start_time = time.time()

        with tracer.start_as_current_span(
            f"{TOOL_CALL_SPAN_NAME}.{tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        ) as span:
            try:
                result = await tool_func(*args, **kwargs)

                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute("autogen.tool.duration_ms", duration_ms)
                span.set_attribute("autogen.tool.result", safe_serialize(result))
                span.set_attribute("autogen.tool.is_error", False)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute("autogen.tool.duration_ms", duration_ms)
                span.set_attribute("autogen.tool.is_error", True)
                span.set_attribute("autogen.tool.error_message", str(e))
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    @functools.wraps(tool_func)
    def sync_wrapper(*args, **kwargs):
        attributes = {
            "autogen.span_kind": "tool_call",
            "autogen.tool.name": tool_name,
        }

        if tool_description:
            attributes["autogen.tool.description"] = tool_description[:500]

        start_time = time.time()

        with tracer.start_as_current_span(
            f"{TOOL_CALL_SPAN_NAME}.{tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        ) as span:
            try:
                result = tool_func(*args, **kwargs)

                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute("autogen.tool.duration_ms", duration_ms)
                span.set_attribute("autogen.tool.result", safe_serialize(result))
                span.set_attribute("autogen.tool.is_error", False)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute("autogen.tool.duration_ms", duration_ms)
                span.set_attribute("autogen.tool.is_error", True)
                span.set_attribute("autogen.tool.error_message", str(e))
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    # Return appropriate wrapper based on function type
    if asyncio.iscoroutinefunction(tool_func):
        return async_wrapper
    else:
        return sync_wrapper


class TracedTeamRunStream:
    """Wrapper for team run_stream that tracks completion."""

    def __init__(self, stream, span, start_time, tracer):
        self._stream = stream
        self._span = span
        self._start_time = start_time
        self._tracer = tracer
        self._message_count = 0
        self._last_result = None

    async def __aenter__(self):
        if hasattr(self._stream, "__aenter__"):
            self._stream = await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self._start_time) * 1000
        self._span.set_attribute("autogen.duration_ms", duration_ms)
        self._span.set_attribute("autogen.stream.message_count", self._message_count)

        if exc_type:
            self._span.set_attribute("autogen.is_error", True)
            self._span.set_attribute("autogen.error.message", str(exc_val))
            self._span.set_status(Status(StatusCode.ERROR, str(exc_val)))
            if exc_val:
                self._span.record_exception(exc_val)
        else:
            self._span.set_attribute("autogen.is_error", False)
            self._span.set_status(Status(StatusCode.OK))

            # Record final result
            if self._last_result:
                if hasattr(self._last_result, "stop_reason"):
                    self._span.set_attribute("autogen.task.stop_reason", str(self._last_result.stop_reason))

        self._span.end()

        if hasattr(self._stream, "__aexit__"):
            await self._stream.__aexit__(exc_type, exc_val, exc_tb)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._stream.__anext__()
        self._message_count += 1
        self._last_result = item
        return item

    # Forward other attributes to underlying stream
    def __getattr__(self, name):
        return getattr(self._stream, name)
