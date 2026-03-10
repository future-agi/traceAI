"""Callback handler for Strands Agents instrumentation.

This module provides a callback handler that captures agent events and
converts them into OpenTelemetry spans, complementing Strands' built-in
telemetry with additional detail.
"""

import json
import time
from typing import Any, Callable, Dict, Optional

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, SpanKind

from traceai_strands._attributes import (
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


class StrandsCallbackHandler:
    """Callback handler for Strands Agents that creates OpenTelemetry spans.

    This handler captures agent lifecycle events and converts them into
    spans, providing detailed observability beyond Strands' built-in
    telemetry.

    Example usage:

        from strands import Agent
        from traceai_strands import StrandsCallbackHandler

        callback = StrandsCallbackHandler(tracer_provider=trace_provider)
        agent = Agent(
            model="us.anthropic.claude-sonnet-4-20250514-v1:0",
            callback_handler=callback,
        )

        response = agent("What is the weather today?")
    """

    def __init__(
        self,
        tracer_provider: Optional[trace.TracerProvider] = None,
        capture_input: bool = True,
        capture_output: bool = True,
    ):
        """Initialize the callback handler.

        Args:
            tracer_provider: Optional OpenTelemetry tracer provider.
            capture_input: Whether to capture input prompts.
            capture_output: Whether to capture output responses.
        """
        if tracer_provider is None:
            tracer_provider = trace.get_tracer_provider()

        self._tracer = tracer_provider.get_tracer(
            "traceai_strands",
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )
        self._capture_input = capture_input
        self._capture_output = capture_output

        # Track active spans for correlation
        self._active_spans: Dict[str, trace.Span] = {}
        self._agent_span: Optional[trace.Span] = None
        self._start_time: Optional[float] = None

    def __call__(self, **kwargs) -> Any:
        """Handle callback events from Strands.

        Strands callbacks receive different kwargs based on the event type.
        Common patterns:
        - data: The event data
        - event: The event type name
        - agent: The agent instance

        Args:
            **kwargs: Event-specific keyword arguments.

        Returns:
            None or callback-specific return value.
        """
        event = kwargs.get("event", "unknown")
        data = kwargs.get("data", {})

        handler = getattr(self, f"_on_{event}", None)
        if handler:
            return handler(data, kwargs)

        # Default handling for unknown events
        return self._on_generic_event(event, data, kwargs)

    def on_agent_start(self, agent: Any, prompt: str) -> None:
        """Called when an agent starts processing.

        Args:
            agent: The Strands Agent instance.
            prompt: The user prompt.
        """
        self._start_time = time.time()

        attrs = get_agent_attributes(agent)
        attrs["gen_ai.operation.name"] = "agent.invoke"

        if self._capture_input:
            attrs["gen_ai.prompt"] = safe_serialize(prompt)

        self._agent_span = self._tracer.start_span(
            name="strands.agent",
            kind=SpanKind.CLIENT,
            attributes=attrs,
        )

    def on_agent_end(self, agent: Any, response: Any) -> None:
        """Called when an agent finishes processing.

        Args:
            agent: The Strands Agent instance.
            response: The agent's response.
        """
        if self._agent_span is None:
            return

        if self._capture_output:
            self._agent_span.set_attribute(
                "gen_ai.completion",
                safe_serialize(response),
            )

        # Calculate duration
        if self._start_time:
            duration = time.time() - self._start_time
            self._agent_span.set_attribute("gen_ai.latency_ms", duration * 1000)

        self._agent_span.set_status(Status(StatusCode.OK))
        self._agent_span.end()
        self._agent_span = None
        self._start_time = None

    def on_agent_error(self, agent: Any, error: Exception) -> None:
        """Called when an agent encounters an error.

        Args:
            agent: The Strands Agent instance.
            error: The exception that occurred.
        """
        if self._agent_span is None:
            return

        self._agent_span.record_exception(error)
        self._agent_span.set_status(
            Status(StatusCode.ERROR, str(error))
        )
        self._agent_span.end()
        self._agent_span = None
        self._start_time = None

    def on_tool_start(self, tool: Any, args: Dict[str, Any]) -> None:
        """Called when a tool starts execution.

        Args:
            tool: The tool being executed.
            args: The arguments passed to the tool.
        """
        tool_name = getattr(tool, "__name__", None) or getattr(tool, "name", "unknown_tool")

        attrs = get_tool_attributes(tool)
        attrs["gen_ai.operation.name"] = "tool.execute"

        if self._capture_input:
            attrs[SpanAttributes.TOOL_PARAMETERS] = safe_serialize(args)

        span = self._tracer.start_span(
            name=f"strands.tool.{tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
        )

        self._active_spans[f"tool:{tool_name}"] = span

    def on_tool_end(self, tool: Any, result: Any) -> None:
        """Called when a tool finishes execution.

        Args:
            tool: The tool that was executed.
            result: The tool's result.
        """
        tool_name = getattr(tool, "__name__", None) or getattr(tool, "name", "unknown_tool")
        span_key = f"tool:{tool_name}"

        span = self._active_spans.pop(span_key, None)
        if span is None:
            return

        if self._capture_output:
            span.set_attribute(SpanAttributes.TOOL_RESULT, safe_serialize(result))

        span.set_status(Status(StatusCode.OK))
        span.end()

    def on_tool_error(self, tool: Any, error: Exception) -> None:
        """Called when a tool encounters an error.

        Args:
            tool: The tool that was executed.
            error: The exception that occurred.
        """
        tool_name = getattr(tool, "__name__", None) or getattr(tool, "name", "unknown_tool")
        span_key = f"tool:{tool_name}"

        span = self._active_spans.pop(span_key, None)
        if span is None:
            return

        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()

    def on_model_start(self, model: Any, messages: Any) -> None:
        """Called when a model inference starts.

        Args:
            model: The model provider instance.
            messages: The messages being sent to the model.
        """
        model_name = getattr(model, "model_id", None) or getattr(model, "model", "unknown")
        provider = get_model_provider(model_name)

        attrs = {
            SpanAttributes.GEN_AI_REQUEST_MODEL: model_name,
            SpanAttributes.STRANDS_MODEL_PROVIDER: provider,
            SpanAttributes.GEN_AI_PROVIDER_NAME: provider,
            "gen_ai.operation.name": "chat",
        }

        if self._capture_input:
            attrs["gen_ai.prompt"] = safe_serialize(messages)

        span = self._tracer.start_span(
            name=f"strands.model.{provider}",
            kind=SpanKind.CLIENT,
            attributes=attrs,
        )

        self._active_spans[f"model:{model_name}"] = span

    def on_model_end(self, model: Any, response: Any) -> None:
        """Called when a model inference completes.

        Args:
            model: The model provider instance.
            response: The model's response.
        """
        model_name = getattr(model, "model_id", None) or getattr(model, "model", "unknown")
        span_key = f"model:{model_name}"

        span = self._active_spans.pop(span_key, None)
        if span is None:
            return

        # Extract usage from response
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

    def on_model_error(self, model: Any, error: Exception) -> None:
        """Called when a model inference fails.

        Args:
            model: The model provider instance.
            error: The exception that occurred.
        """
        model_name = getattr(model, "model_id", None) or getattr(model, "model", "unknown")
        span_key = f"model:{model_name}"

        span = self._active_spans.pop(span_key, None)
        if span is None:
            return

        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()

    def on_streaming_start(self, agent: Any) -> None:
        """Called when streaming response starts.

        Args:
            agent: The Strands Agent instance.
        """
        span = self._tracer.start_span(
            name="strands.stream",
            kind=SpanKind.INTERNAL,
            attributes={"gen_ai.operation.name": "stream"},
        )
        self._active_spans["stream"] = span

    def on_streaming_chunk(self, chunk: Any) -> None:
        """Called for each streaming chunk.

        Args:
            chunk: The streaming chunk data.
        """
        span = self._active_spans.get("stream")
        if span:
            # Increment chunk counter
            current_count = span.attributes.get("gen_ai.stream.chunk_count", 0)
            span.set_attribute("gen_ai.stream.chunk_count", current_count + 1)

    def on_streaming_end(self, agent: Any, response: Any) -> None:
        """Called when streaming response completes.

        Args:
            agent: The Strands Agent instance.
            response: The complete response.
        """
        span = self._active_spans.pop("stream", None)
        if span:
            if self._capture_output:
                span.set_attribute("gen_ai.completion", safe_serialize(response))
            span.set_status(Status(StatusCode.OK))
            span.end()

    def _on_generic_event(
        self, event: str, data: Any, kwargs: Dict[str, Any]
    ) -> None:
        """Handle generic/unknown events.

        Args:
            event: The event name.
            data: The event data.
            kwargs: Additional keyword arguments.
        """
        # Create a span for unknown events for debugging
        with self._tracer.start_as_current_span(
            name=f"strands.event.{event}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("strands.event.type", event)
            if data:
                span.set_attribute("strands.event.data", safe_serialize(data, max_length=1000))


def create_callback_handler(
    tracer_provider: Optional[trace.TracerProvider] = None,
    capture_input: bool = True,
    capture_output: bool = True,
) -> StrandsCallbackHandler:
    """Create a configured callback handler.

    This is a convenience function for creating a StrandsCallbackHandler
    with common configuration options.

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider.
        capture_input: Whether to capture input prompts.
        capture_output: Whether to capture output responses.

    Returns:
        Configured StrandsCallbackHandler instance.
    """
    return StrandsCallbackHandler(
        tracer_provider=tracer_provider,
        capture_input=capture_input,
        capture_output=capture_output,
    )
