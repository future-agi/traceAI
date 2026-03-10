"""Middleware for BeeAI Framework instrumentation.

This module provides middleware classes that can be added to BeeAI agents
for extended tracing and observability beyond the built-in OpenInference
instrumentation.
"""

import json
import time
from typing import Any, Callable, Dict, List, Optional

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, SpanKind

from traceai_beeai._attributes import (
    SpanAttributes,
    get_agent_attributes,
    get_tool_attributes,
    get_model_provider,
)


def safe_serialize(obj: Any, max_length: int = 10000) -> str:
    """Safely serialize an object to a string.

    Args:
        obj: Object to serialize.
        max_length: Maximum length of the result.

    Returns:
        String representation of the object.
    """
    if obj is None:
        return "null"

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


class TraceAIMiddleware:
    """Middleware for BeeAI agents that adds extended tracing.

    This middleware complements BeeAI's built-in OpenInference instrumentation
    with additional spans and attributes specific to TraceAI.

    Example usage:

        from beeai_framework.agents import Agent
        from traceai_beeai import TraceAIMiddleware

        middleware = TraceAIMiddleware(tracer_provider=trace_provider)

        agent = Agent(
            llm=model,
            tools=[...],
            middlewares=[middleware],
        )
    """

    def __init__(
        self,
        tracer_provider: Optional[trace.TracerProvider] = None,
        capture_input: bool = True,
        capture_output: bool = True,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        """Initialize the middleware.

        Args:
            tracer_provider: Optional OpenTelemetry tracer provider.
            capture_input: Whether to capture input data.
            capture_output: Whether to capture output data.
            session_id: Optional session identifier for all spans.
            user_id: Optional user identifier for all spans.
        """
        if tracer_provider is None:
            tracer_provider = trace.get_tracer_provider()

        self._tracer = tracer_provider.get_tracer(
            "traceai_beeai",
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )
        self._capture_input = capture_input
        self._capture_output = capture_output
        self._session_id = session_id
        self._user_id = user_id

        # Track active spans
        self._active_spans: Dict[str, trace.Span] = {}

    def on_agent_start(
        self,
        agent: Any,
        input_data: Any,
    ) -> None:
        """Called when an agent starts processing.

        Args:
            agent: The BeeAI agent instance.
            input_data: The input to the agent.
        """
        attrs = get_agent_attributes(agent)
        attrs["gen_ai.operation.name"] = "agent.run"

        if self._session_id:
            attrs[SpanAttributes.BEEAI_SESSION_ID] = self._session_id
        if self._user_id:
            attrs[SpanAttributes.BEEAI_USER_ID] = self._user_id

        if self._capture_input and input_data is not None:
            attrs["gen_ai.prompt"] = safe_serialize(input_data)

        span = self._tracer.start_span(
            name=f"beeai.agent.{type(agent).__name__}",
            kind=SpanKind.CLIENT,
            attributes=attrs,
        )

        self._active_spans["agent"] = span

    def on_agent_end(
        self,
        agent: Any,
        output: Any,
    ) -> None:
        """Called when an agent finishes processing.

        Args:
            agent: The BeeAI agent instance.
            output: The agent's output.
        """
        span = self._active_spans.pop("agent", None)
        if span is None:
            return

        if self._capture_output and output is not None:
            span.set_attribute("gen_ai.completion", safe_serialize(output))

        span.set_status(Status(StatusCode.OK))
        span.end()

    def on_agent_error(
        self,
        agent: Any,
        error: Exception,
    ) -> None:
        """Called when an agent encounters an error.

        Args:
            agent: The BeeAI agent instance.
            error: The exception that occurred.
        """
        span = self._active_spans.pop("agent", None)
        if span is None:
            return

        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()

    def on_tool_start(
        self,
        tool: Any,
        args: Dict[str, Any],
    ) -> None:
        """Called when a tool starts execution.

        Args:
            tool: The tool being executed.
            args: The arguments passed to the tool.
        """
        tool_name = getattr(tool, "name", None) or getattr(tool, "__name__", "unknown")

        attrs = get_tool_attributes(tool)
        attrs["gen_ai.operation.name"] = "tool.execute"

        if self._capture_input:
            attrs[SpanAttributes.TOOL_PARAMETERS] = safe_serialize(args)

        span = self._tracer.start_span(
            name=f"beeai.tool.{tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
        )

        self._active_spans[f"tool:{tool_name}"] = span

    def on_tool_end(
        self,
        tool: Any,
        result: Any,
    ) -> None:
        """Called when a tool finishes execution.

        Args:
            tool: The tool that was executed.
            result: The tool's result.
        """
        tool_name = getattr(tool, "name", None) or getattr(tool, "__name__", "unknown")
        span_key = f"tool:{tool_name}"

        span = self._active_spans.pop(span_key, None)
        if span is None:
            return

        if self._capture_output:
            span.set_attribute(SpanAttributes.TOOL_RESULT, safe_serialize(result))

        span.set_status(Status(StatusCode.OK))
        span.end()

    def on_tool_error(
        self,
        tool: Any,
        error: Exception,
    ) -> None:
        """Called when a tool encounters an error.

        Args:
            tool: The tool that was executed.
            error: The exception that occurred.
        """
        tool_name = getattr(tool, "name", None) or getattr(tool, "__name__", "unknown")
        span_key = f"tool:{tool_name}"

        span = self._active_spans.pop(span_key, None)
        if span is None:
            return

        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()

    def on_llm_start(
        self,
        model: Any,
        messages: Any,
    ) -> None:
        """Called when an LLM call starts.

        Args:
            model: The LLM/model instance.
            messages: The messages being sent.
        """
        model_name = getattr(model, "model", None) or getattr(model, "model_id", "unknown")
        provider = get_model_provider(model_name)

        attrs = {
            SpanAttributes.GEN_AI_REQUEST_MODEL: model_name,
            SpanAttributes.GEN_AI_PROVIDER_NAME: provider,
            "gen_ai.operation.name": "chat",
        }

        if self._capture_input:
            attrs["gen_ai.prompt"] = safe_serialize(messages)

        span = self._tracer.start_span(
            name=f"beeai.llm.{provider}",
            kind=SpanKind.CLIENT,
            attributes=attrs,
        )

        self._active_spans[f"llm:{model_name}"] = span

    def on_llm_end(
        self,
        model: Any,
        response: Any,
    ) -> None:
        """Called when an LLM call completes.

        Args:
            model: The LLM/model instance.
            response: The model's response.
        """
        model_name = getattr(model, "model", None) or getattr(model, "model_id", "unknown")
        span_key = f"llm:{model_name}"

        span = self._active_spans.pop(span_key, None)
        if span is None:
            return

        # Extract usage
        usage = getattr(response, "usage", None)
        if usage:
            input_tokens = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", None)

            if input_tokens is not None:
                span.set_attribute(SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
            if output_tokens is not None:
                span.set_attribute(SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)
            if input_tokens and output_tokens:
                span.set_attribute(SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, input_tokens + output_tokens)

        if self._capture_output:
            content = getattr(response, "content", None) or getattr(response, "text", None)
            if content:
                span.set_attribute("gen_ai.completion", safe_serialize(content))

        span.set_status(Status(StatusCode.OK))
        span.end()

    def on_llm_error(
        self,
        model: Any,
        error: Exception,
    ) -> None:
        """Called when an LLM call fails.

        Args:
            model: The LLM/model instance.
            error: The exception that occurred.
        """
        model_name = getattr(model, "model", None) or getattr(model, "model_id", "unknown")
        span_key = f"llm:{model_name}"

        span = self._active_spans.pop(span_key, None)
        if span is None:
            return

        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()

    def on_workflow_step(
        self,
        workflow: Any,
        step_name: str,
        step_data: Any = None,
    ) -> None:
        """Record a workflow step.

        Args:
            workflow: The workflow instance.
            step_name: Name of the current step.
            step_data: Optional data for the step.
        """
        workflow_name = getattr(workflow, "name", None) or "workflow"

        attrs = {
            SpanAttributes.BEEAI_WORKFLOW_NAME: workflow_name,
            SpanAttributes.BEEAI_WORKFLOW_STEP: step_name,
            "gen_ai.operation.name": "workflow.step",
        }

        if step_data is not None:
            attrs["beeai.workflow.step_data"] = safe_serialize(step_data, max_length=1000)

        with self._tracer.start_as_current_span(
            name=f"beeai.workflow.{step_name}",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
        ) as span:
            span.set_status(Status(StatusCode.OK))


def create_tracing_middleware(
    tracer_provider: Optional[trace.TracerProvider] = None,
    capture_input: bool = True,
    capture_output: bool = True,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> TraceAIMiddleware:
    """Create a configured TraceAI middleware for BeeAI agents.

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider.
        capture_input: Whether to capture input data.
        capture_output: Whether to capture output data.
        session_id: Optional session identifier.
        user_id: Optional user identifier.

    Returns:
        Configured TraceAIMiddleware instance.
    """
    return TraceAIMiddleware(
        tracer_provider=tracer_provider,
        capture_input=capture_input,
        capture_output=capture_output,
        session_id=session_id,
        user_id=user_id,
    )
