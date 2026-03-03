"""Agent wrapper for Pydantic AI instrumentation.

This module provides wrapping for Pydantic AI Agent class to add
comprehensive tracing beyond the built-in instrumentation.
"""

import functools
import json
import logging
import time
import uuid
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


def extract_usage(result: Any) -> Optional[Dict[str, Any]]:
    """Extract usage information from a run result.

    Args:
        result: RunResult or similar object

    Returns:
        Dict with usage info or None
    """
    usage = getattr(result, "usage", None)
    if not usage:
        return None

    return {
        "input_tokens": getattr(usage, "request_tokens", 0)
        or getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "response_tokens", 0)
        or getattr(usage, "output_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
        "requests": getattr(usage, "requests", 0),
    }


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
        # Generate run ID
        run_id = str(uuid.uuid4())

        # Extract prompt
        prompt = ""
        if args:
            prompt = safe_serialize(args[0])
        elif "user_prompt" in kwargs:
            prompt = safe_serialize(kwargs["user_prompt"])

        # Get model info
        model_info = extract_model_info(self)

        # Build attributes
        attributes = {
            PydanticAIAttributes.SPAN_KIND: PydanticAISpanKind.AGENT_RUN.value,
            PydanticAIAttributes.RUN_ID: run_id,
            PydanticAIAttributes.RUN_METHOD: method_name,
            PydanticAIAttributes.RUN_PROMPT: prompt,
        }

        # Add agent info
        if hasattr(self, "name") and self.name:
            attributes[PydanticAIAttributes.GEN_AI_AGENT_NAME] = self.name

        # Add model info
        if model_info.get("model_name"):
            attributes[PydanticAIAttributes.MODEL_NAME] = model_info["model_name"]
        if model_info.get("model_provider"):
            attributes[PydanticAIAttributes.MODEL_PROVIDER] = model_info["model_provider"]

        # Add instructions if available
        instructions = getattr(self, "instructions", None)
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
        result_type = getattr(self, "result_type", None)
        if result_type:
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

        start_time = time.time()

        with tracer.start_as_current_span(
            AGENT_RUN_SPAN_NAME,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = await original_method(self, *args, **kwargs)

                # Record result
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)

                # Extract and record usage
                usage = extract_usage(result)
                if usage:
                    span.set_attribute(
                        PydanticAIAttributes.USAGE_INPUT_TOKENS,
                        usage.get("input_tokens", 0),
                    )
                    span.set_attribute(
                        PydanticAIAttributes.USAGE_OUTPUT_TOKENS,
                        usage.get("output_tokens", 0),
                    )
                    span.set_attribute(
                        PydanticAIAttributes.USAGE_TOTAL_TOKENS,
                        usage.get("total_tokens", 0),
                    )

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
        # Generate run ID
        run_id = str(uuid.uuid4())

        # Extract prompt
        prompt = ""
        if args:
            prompt = safe_serialize(args[0])
        elif "user_prompt" in kwargs:
            prompt = safe_serialize(kwargs["user_prompt"])

        # Get model info
        model_info = extract_model_info(self)

        # Build attributes
        attributes = {
            PydanticAIAttributes.SPAN_KIND: PydanticAISpanKind.AGENT_RUN.value,
            PydanticAIAttributes.RUN_ID: run_id,
            PydanticAIAttributes.RUN_METHOD: method_name,
            PydanticAIAttributes.RUN_PROMPT: prompt,
        }

        # Add model info
        if model_info.get("model_name"):
            attributes[PydanticAIAttributes.MODEL_NAME] = model_info["model_name"]
        if model_info.get("model_provider"):
            attributes[PydanticAIAttributes.MODEL_PROVIDER] = model_info["model_provider"]

        start_time = time.time()

        with tracer.start_as_current_span(
            AGENT_RUN_SPAN_NAME,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = original_method(self, *args, **kwargs)

                # Record result
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)

                # Extract and record usage
                usage = extract_usage(result)
                if usage:
                    span.set_attribute(
                        PydanticAIAttributes.USAGE_INPUT_TOKENS,
                        usage.get("input_tokens", 0),
                    )
                    span.set_attribute(
                        PydanticAIAttributes.USAGE_OUTPUT_TOKENS,
                        usage.get("output_tokens", 0),
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
