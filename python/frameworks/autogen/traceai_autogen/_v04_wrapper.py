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

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import MessageAttributes, SpanAttributes
from opentelemetry import context as context_api, trace as trace_api
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


def _mask_content(config, value, output=False):
    """Keep historical aliases, but apply the canonical content policy first.

    Aliases are not recognized by FITracer itself. Both the value and message
    policies apply so a narrower hide-text/messages flag cannot leak via them.
    """
    message_key = (SpanAttributes.GEN_AI_OUTPUT_MESSAGES if output else
                   SpanAttributes.GEN_AI_INPUT_MESSAGES) + ".0." + MessageAttributes.MESSAGE_CONTENT
    masked = config.mask(message_key, value)
    if masked is None:
        return "__REDACTED__"
    return config.mask(SpanAttributes.OUTPUT_VALUE if output else SpanAttributes.INPUT_VALUE, masked)


def _agent_attributes(agent, messages, config):
    info = extract_agent_info(agent)
    attributes = {
        "autogen.span_kind": "agent_run",
        "autogen.run.id": str(uuid.uuid4()),
        "autogen.agent.name": info["agent_name"],
        "autogen.agent.type": info["agent_type"],
    }
    if info.get("model_name"):
        attributes["gen_ai.request.model"] = info["model_name"]
    if info.get("tool_count"):
        attributes["autogen.agent.tool_count"] = info["tool_count"]
    if messages:
        attributes["autogen.input.message_count"] = len(messages)
        last = extract_message_info(messages[-1])
        attributes["autogen.input.last_message"] = _mask_content(config, last.get("content", ""))
        attributes["autogen.input.last_source"] = last.get("source", "")
    return attributes


def _record_agent_result(span, result, config):
    if result is not None and hasattr(result, "chat_message"):
        info = extract_message_info(result.chat_message)
        span.set_attribute("autogen.output.message_type", info.get("message_type", ""))
        span.set_attribute("autogen.output.content", _mask_content(config, info.get("content", ""), output=True))
        if getattr(result, "inner_messages", None):
            span.set_attribute("autogen.output.inner_message_count", len(result.inner_messages))


def _team_attributes(team, task, method, config):
    info = extract_team_info(team)
    attributes = {
        "autogen.span_kind": "team_run",
        "autogen.run.id": str(uuid.uuid4()),
        "autogen.team.type": info["team_type"],
        "autogen.run.method": method,
    }
    for key in ("participant_count", "termination_condition", "max_turns"):
        if info.get(key):
            attributes[f"autogen.team.{key}"] = info[key]
    if info.get("participants"):
        attributes["autogen.team.participants"] = json.dumps(info["participants"])
    if task:
        attributes["autogen.task.content"] = _mask_content(config, safe_serialize(task, 1000))
    return attributes


def _record_team_result(span, result, config):
    if result is None:
        return
    if hasattr(result, "messages"):
        span.set_attribute("autogen.task.message_count", len(result.messages))
    if hasattr(result, "stop_reason"):
        # TextMentionTermination quotes model text in the stop reason too.
        span.set_attribute("autogen.task.stop_reason", _mask_content(config, str(result.stop_reason), output=True))
    usage = extract_usage_from_result(result)
    if usage:
        for key, value in usage.items():
            span.set_attribute(f"gen_ai.usage.{key}", value)


def _record_error(span, error, config):
    # Supported published cores predate FITracer's event/status sanitization.
    # Apply the derived policy locally, respecting explicit overrides on newer
    # cores, without upgrading the dependency or changing the SDK exception.
    hide_details = getattr(config, "hide_error_details", None)
    if hide_details is None:
        hide_details = any(getattr(config, flag, False) for flag in (
            "hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages",
            "hide_input_text", "hide_output_text",
        ))
    span.set_attribute("autogen.is_error", True)
    span.set_attribute("autogen.error.type", type(error).__name__)
    if hide_details:
        span.set_status(Status(StatusCode.ERROR))
        span.add_event("exception", {"exception.type": type(error).__name__})
    else:
        span.set_attribute("autogen.error.message", str(error))
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.record_exception(error)


def _wrap_stream(original_method, tracer, name, attributes, record_result, kind, config):
    @functools.wraps(original_method)
    async def stream_wrapper(self, *args, **kwargs):
        # Remain a true async generator, forwarding each consumer operation once.
        # No prefetch: Response may precede slow/failing SDK post-yield cleanup.
        span = tracer.start_span(name(self), kind=kind, attributes=attributes(self, args, kwargs))
        start_time = time.time()
        stream = None
        count = 0
        ended = False
        producer_context = trace_api.set_span_in_context(span)

        def finish():
            nonlocal ended
            if not ended:
                span.set_attribute("autogen.stream.message_count", count)
                span.set_attribute("autogen.duration_ms", (time.time() - start_time) * 1000)
                span.end()
                ended = True

        def record_error(error):
            if not ended:
                _record_error(span, error, config)
                return
            # Convenience on_messages callers may abandon iteration at Response,
            # so the logical result must already be exported. A later requested
            # advance/close can still fail: retain that failure on a linked child
            # rather than mutating an ended span or prefetching SDK cleanup.
            cleanup = tracer.start_span(
                name(self) + ".cleanup", kind=SpanKind.INTERNAL,
                context=trace_api.set_span_in_context(span),
                links=[trace_api.Link(span.get_span_context())],
                attributes={"autogen.stream.cleanup": True},
            )
            try:
                _record_error(cleanup, error, config)
            finally:
                cleanup.end()

        async def advance(operation, *values):
            nonlocal producer_context
            # Preserve an SDK child context suspended across its yield, but
            # always restore this consumer's context at the proxy boundary.
            token = context_api.attach(producer_context)
            try:
                return await operation(*values)
            finally:
                producer_context = context_api.get_current()
                context_api.detach(token)

        try:
            stream = original_method(self, *args, **kwargs).__aiter__()
            operation, values = stream.__anext__, ()
            while True:
                try:
                    item = await advance(operation, *values)
                except StopAsyncIteration:
                    stream = None
                    if not ended:
                        span.set_attribute("autogen.is_error", False)
                        span.set_status(Status(StatusCode.OK))
                    break
                count += 1
                if not ended:
                    record_result(span, item)
                    from autogen_agentchat.base import Response, TaskResult
                    if isinstance(item, (Response, TaskResult)):
                        # Logical result completes tracing, NOT iteration. Some
                        # on_messages callers return without resuming the stream.
                        # Leave post-result work suspended until asked to resume.
                        span.set_attribute("autogen.is_error", False)
                        span.set_status(Status(StatusCode.OK))
                        finish()
                try:
                    sent = yield item
                except BaseException as error:
                    # This includes GeneratorExit: forwarding the injection lets
                    # the SDK catch it for athrow(), while Python's aclose()
                    # still enforces its native "must not yield" rule.
                    if isinstance(error, GeneratorExit) and not ended:
                        span.set_attribute("autogen.stream.closed_early", True)
                    operation, values = stream.athrow, (error,)
                else:
                    operation, values = (stream.__anext__, ()) if sent is None else (stream.asend, (sent,))
        except GeneratorExit:
            if not ended:
                span.set_attribute("autogen.stream.closed_early", True)
            raise
        except BaseException as error:
            record_error(error)
            raise
        finally:
            try:
                if stream is not None and hasattr(stream, "aclose"):
                    await advance(stream.aclose)
            except BaseException as error:
                record_error(error)
                raise
            finally:
                finish()
    return stream_wrapper


def wrap_agent_on_messages(original_method: Callable, tracer: trace_api.Tracer, config=None) -> Callable:
    config = config or TraceConfig()
    @functools.wraps(original_method)
    async def async_wrapper(self, *args, **kwargs):
        messages = kwargs.get("messages", args[0] if args else None)
        start_time = time.time()
        with tracer.start_as_current_span(
            f"{AGENT_ON_MESSAGES_SPAN_NAME}.{getattr(self, 'name', 'agent')}",
            kind=SpanKind.INTERNAL, attributes=_agent_attributes(self, messages, config),
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                result = await original_method(self, *args, **kwargs)
                _record_agent_result(span, result, config)
                span.set_attribute("autogen.is_error", False)
                span.set_status(Status(StatusCode.OK))
                return result
            except BaseException as error:
                _record_error(span, error, config)
                raise
            finally:
                span.set_attribute("autogen.duration_ms", (time.time() - start_time) * 1000)
    return async_wrapper


def wrap_agent_on_messages_stream(original_method: Callable, tracer: trace_api.Tracer, config=None) -> Callable:
    config = config or TraceConfig()
    return _wrap_stream(
        original_method, tracer,
        lambda agent: f"autogen.agent.on_messages_stream.{getattr(agent, 'name', 'agent')}",
        lambda agent, args, kwargs: _agent_attributes(agent, kwargs.get("messages", args[0] if args else None), config),
        lambda span, result: _record_agent_result(span, result, config), SpanKind.INTERNAL, config,
    )


def wrap_team_run(original_method: Callable, tracer: trace_api.Tracer, method_name: str = "run", config=None) -> Callable:
    config = config or TraceConfig()
    if method_name == "run_stream":
        return _wrap_stream(
            original_method, tracer,
            lambda team: f"{TEAM_RUN_STREAM_SPAN_NAME}.{type(team).__name__}",
            lambda team, args, kwargs: _team_attributes(team, kwargs.get("task", args[0] if args else None), method_name, config),
            lambda span, result: _record_team_result(span, result, config), SpanKind.CLIENT, config,
        )

    @functools.wraps(original_method)
    async def async_wrapper(self, *args, **kwargs):
        task = kwargs.get("task", args[0] if args else None)
        start_time = time.time()
        with tracer.start_as_current_span(
            f"{TEAM_RUN_SPAN_NAME}.{type(self).__name__}", kind=SpanKind.CLIENT,
            attributes=_team_attributes(self, task, method_name, config),
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                # AgentChat's task is keyword-only and optional. Preserve the
                # entire call shape, including task-less continuation.
                result = await original_method(self, *args, **kwargs)
                _record_team_result(span, result, config)
                span.set_attribute("autogen.is_error", False)
                span.set_status(Status(StatusCode.OK))
                return result
            except BaseException as error:
                _record_error(span, error, config)
                raise
            finally:
                span.set_attribute("autogen.duration_ms", (time.time() - start_time) * 1000)
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
