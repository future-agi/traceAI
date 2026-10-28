"""Agent wrapper for Pydantic AI instrumentation.

This module provides wrapping for Pydantic AI Agent class to add
comprehensive tracing beyond the built-in instrumentation.
"""

import functools
import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional, TypeVar, Union

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from ._attributes import (
    PydanticAIAttributes,
    PydanticAISpanKind,
    get_model_provider,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Span names
AGENT_RUN_SPAN_NAME = "pydantic_ai.agent.run"
MODEL_REQUEST_SPAN_NAME = "pydantic_ai.model.request"
TOOL_CALL_SPAN_NAME = "pydantic_ai.tool.call"

# run_sync delegates to run in a copied async context. Suppress only that
# delegation, not nested agent operations or tracing in unrelated tasks.
_SYNC_DELEGATION: ContextVar[Any] = ContextVar("pydantic_ai_sync_delegation", default=None)


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


def extract_model_info(agent: Any) -> Dict[str, Any]:
    """Extract model information from an agent.

    Args:
        agent: Pydantic AI Agent instance

    Returns:
        Dict with model info
    """
    info = {}

    # Try to get model from agent
    model = getattr(agent, "model", None)
    if model:
        if isinstance(model, str):
            info["model_name"] = model
            info["model_provider"] = get_model_provider(model)
        elif hasattr(model, "model_name"):
            info["model_name"] = model.model_name
            info["model_provider"] = get_model_provider(model.model_name)
        elif hasattr(model, "name"):
            info["model_name"] = model.name
            info["model_provider"] = get_model_provider(model.name)

    return info


def _usage_values(usage: Any) -> Optional[Dict[str, Any]]:
    """Copy known counters; None means unknown, not measured zero."""
    if usage is None:
        return None
    values = {}
    for key, names in {
        "input_tokens": ("request_tokens", "input_tokens"),
        "output_tokens": ("response_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
        "requests": ("requests",),
    }.items():
        for name in names:
            value = getattr(usage, name, None)
            if isinstance(value, int):
                values[key] = value
                break
    return values


def _sum_usage(samples):
    values = {}
    for sample in samples:
        for key, value in (sample or {}).items():
            values[key] = values.get(key, 0) + value
    return values


class _OperationUsage:
    def __init__(self, budget):
        self.budget = budget
        self.previous = _usage_values(budget)
        self.samples = []

    def extract(self, result):
        if self.samples:
            return _sum_usage(self.samples)
        return extract_usage(result, self.previous)


_ACTIVE_USAGE: ContextVar[Any] = ContextVar("pydantic_ai_operation_usage", default=None)


def wrap_legacy_usage_incr(original):
    """Historical responses omit usage. Observe only this run's budget updates.

    Keep the SDK's real shared budget intact (including limits and identity),
    and never infer usage from changes made by another task or nested run.
    """
    @functools.wraps(original)
    def incr(self, incr_usage, *args, **kwargs):
        operation = _ACTIVE_USAGE.get()
        sample = _usage_values(incr_usage)
        result = original(self, incr_usage, *args, **kwargs)
        if operation is not None and operation.budget is self:
            operation.samples.append(sample)
        return result
    return incr


def extract_usage(
    result: Any, previous_usage: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Prefer run-local response counters over a mutable aggregate budget."""
    new_messages = getattr(result, "new_messages", None)
    if callable(new_messages):
        messages = new_messages()
        if isinstance(messages, (list, tuple)):
            responses = [m for m in messages if getattr(m, "kind", None) == "response"]
            if responses and all(getattr(m, "usage", None) is not None for m in responses):
                return _sum_usage(_usage_values(m.usage) for m in responses)

    # Historical SDKs expose usage() and no counters on ModelResponse. Shared
    # budgets use _OperationUsage samples above; unshared results are run-local.
    usage = getattr(result, "usage", None)
    if callable(usage):
        usage = usage()
    values = _usage_values(usage)
    if values is not None and previous_usage is not None:
        return {key: value - previous_usage.get(key, 0) for key, value in values.items()}
    return values


def _record_usage(span, usage):
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if usage is not None and key in usage:
            span.set_attribute(f"gen_ai.usage.{key}", usage[key])


def _run_attributes(
    agent: Any, args: tuple, kwargs: dict, method_name: str
) -> Dict[str, Any]:
    """Keep sync and async operation attributes identical."""
    # Generate run ID
    run_id = str(uuid.uuid4())

    # Extract prompt
    prompt = ""
    if args:
        prompt = safe_serialize(args[0])
    elif "user_prompt" in kwargs:
        prompt = safe_serialize(kwargs["user_prompt"])

    # Get model info
    model_info = extract_model_info(agent)

    # Build attributes
    attributes = {
        PydanticAIAttributes.SPAN_KIND: PydanticAISpanKind.AGENT_RUN.value,
        PydanticAIAttributes.RUN_ID: run_id,
        PydanticAIAttributes.RUN_METHOD: method_name,
        PydanticAIAttributes.RUN_PROMPT: prompt,
    }

    # Add agent info
    if hasattr(agent, "name") and agent.name:
        attributes[PydanticAIAttributes.GEN_AI_AGENT_NAME] = agent.name

    # Add model info
    if model_info.get("model_name"):
        attributes[PydanticAIAttributes.MODEL_NAME] = model_info["model_name"]
    if model_info.get("model_provider"):
        attributes[PydanticAIAttributes.MODEL_PROVIDER] = model_info["model_provider"]

    # Add instructions if available
    instructions = getattr(agent, "instructions", None)
    if instructions:
        if callable(instructions):
            try:
                instr_str = str(instructions)
            except Exception:
                instr_str = "<dynamic>"
        else:
            instr_str = safe_serialize(instructions, 500)
        attributes[PydanticAIAttributes.AGENT_INSTRUCTIONS] = instr_str

    # Add result type info
    result_type = (
        kwargs.get("output_type")
        or getattr(agent, "result_type", None)
        or getattr(agent, "output_type", None)
    )
    if result_type and result_type is not str:
        attributes[PydanticAIAttributes.AGENT_RESULT_TYPE] = str(result_type)
        attributes[PydanticAIAttributes.RUN_IS_STRUCTURED] = True
    else:
        attributes[PydanticAIAttributes.RUN_IS_STRUCTURED] = False

    # Check for message history
    message_history = kwargs.get("message_history")
    if message_history:
        attributes[PydanticAIAttributes.RUN_MESSAGE_HISTORY_LENGTH] = len(
            message_history
        )

    return attributes


def wrap_agent_run(
    original_method: Callable,
    tracer: trace_api.Tracer,
    method_name: str,
) -> Callable:
    """Wrap an agent run method with tracing.

    Args:
        original_method: Original run method
        tracer: OpenTelemetry tracer
        method_name: Name of the method (run, run_sync, etc.)

    Returns:
        Wrapped method
    """

    @functools.wraps(original_method)
    async def async_wrapper(self, *args, **kwargs):
        if method_name == "run" and _SYNC_DELEGATION.get() is self:
            token = _SYNC_DELEGATION.set(None)
            try:
                return await original_method(self, *args, **kwargs)
            finally:
                _SYNC_DELEGATION.reset(token)

        attributes = _run_attributes(self, args, kwargs, method_name)
        operation_usage = _OperationUsage(kwargs.get("usage"))

        start_time = time.time()

        with tracer.start_as_current_span(
            AGENT_RUN_SPAN_NAME,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                usage_token = _ACTIVE_USAGE.set(operation_usage)
                try:
                    result = await original_method(self, *args, **kwargs)
                finally:
                    _ACTIVE_USAGE.reset(usage_token)

                # Record result
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)

                # Extract and record usage
                _record_usage(span, operation_usage.extract(result))

                # Record output
                output = getattr(result, "output", None) or getattr(result, "data", None)
                if output is not None:
                    span.set_attribute(
                        PydanticAIAttributes.RUN_RESULT,
                        safe_serialize(output),
                    )

                span.set_attribute(PydanticAIAttributes.IS_ERROR, False)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)
                span.set_attribute(PydanticAIAttributes.IS_ERROR, True)
                span.set_attribute(PydanticAIAttributes.ERROR_TYPE, type(e).__name__)
                span.set_attribute(PydanticAIAttributes.ERROR_MESSAGE, str(e))
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    @functools.wraps(original_method)
    def sync_wrapper(self, *args, **kwargs):
        attributes = _run_attributes(self, args, kwargs, method_name)
        operation_usage = _OperationUsage(kwargs.get("usage"))

        start_time = time.time()

        with tracer.start_as_current_span(
            AGENT_RUN_SPAN_NAME,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                token = _SYNC_DELEGATION.set(self)
                usage_token = _ACTIVE_USAGE.set(operation_usage)
                try:
                    result = original_method(self, *args, **kwargs)
                finally:
                    _ACTIVE_USAGE.reset(usage_token)
                    _SYNC_DELEGATION.reset(token)

                # Record result
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)

                # Extract and record usage
                _record_usage(span, operation_usage.extract(result))

                output = getattr(result, "output", None)
                if output is None:
                    output = getattr(result, "data", None)
                if output is not None:
                    span.set_attribute(
                        PydanticAIAttributes.RUN_RESULT, safe_serialize(output)
                    )

                span.set_attribute(PydanticAIAttributes.IS_ERROR, False)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)
                span.set_attribute(PydanticAIAttributes.IS_ERROR, True)
                span.set_attribute(PydanticAIAttributes.ERROR_TYPE, type(e).__name__)
                span.set_attribute(PydanticAIAttributes.ERROR_MESSAGE, str(e))
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    # Return appropriate wrapper based on method type
    if method_name == "run_sync":
        return sync_wrapper
    else:
        return async_wrapper


def wrap_tool_function(
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

    @functools.wraps(tool_func)
    async def async_wrapper(*args, **kwargs):
        attributes = {
            PydanticAIAttributes.SPAN_KIND: PydanticAISpanKind.TOOL_CALL.value,
            PydanticAIAttributes.GEN_AI_TOOL_NAME: tool_name,
        }

        if tool_description:
            attributes[PydanticAIAttributes.GEN_AI_TOOL_DESCRIPTION] = tool_description[:500]

        # Serialize args (skip RunContext which is typically first arg)
        if len(args) > 1:
            attributes[PydanticAIAttributes.TOOL_ARGS] = safe_serialize(args[1:])
        if kwargs:
            if PydanticAIAttributes.TOOL_ARGS in attributes:
                attributes[PydanticAIAttributes.TOOL_ARGS] += f", {safe_serialize(kwargs)}"
            else:
                attributes[PydanticAIAttributes.TOOL_ARGS] = safe_serialize(kwargs)

        start_time = time.time()

        with tracer.start_as_current_span(
            f"{TOOL_CALL_SPAN_NAME}.{tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        ) as span:
            try:
                result = await tool_func(*args, **kwargs)

                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.TOOL_DURATION_MS, duration_ms)
                span.set_attribute(
                    PydanticAIAttributes.TOOL_RESULT,
                    safe_serialize(result),
                )
                span.set_attribute(PydanticAIAttributes.TOOL_IS_ERROR, False)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.TOOL_DURATION_MS, duration_ms)
                span.set_attribute(PydanticAIAttributes.TOOL_IS_ERROR, True)
                span.set_attribute(PydanticAIAttributes.TOOL_ERROR_MESSAGE, str(e))
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    @functools.wraps(tool_func)
    def sync_wrapper(*args, **kwargs):
        attributes = {
            PydanticAIAttributes.SPAN_KIND: PydanticAISpanKind.TOOL_CALL.value,
            PydanticAIAttributes.GEN_AI_TOOL_NAME: tool_name,
        }

        if tool_description:
            attributes[PydanticAIAttributes.GEN_AI_TOOL_DESCRIPTION] = tool_description[:500]

        start_time = time.time()

        with tracer.start_as_current_span(
            f"{TOOL_CALL_SPAN_NAME}.{tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        ) as span:
            try:
                result = tool_func(*args, **kwargs)

                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.TOOL_DURATION_MS, duration_ms)
                span.set_attribute(
                    PydanticAIAttributes.TOOL_RESULT,
                    safe_serialize(result),
                )
                span.set_attribute(PydanticAIAttributes.TOOL_IS_ERROR, False)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.TOOL_DURATION_MS, duration_ms)
                span.set_attribute(PydanticAIAttributes.TOOL_IS_ERROR, True)
                span.set_attribute(PydanticAIAttributes.TOOL_ERROR_MESSAGE, str(e))
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    # Check if function is async
    import asyncio

    if asyncio.iscoroutinefunction(tool_func):
        return async_wrapper
    else:
        return sync_wrapper
