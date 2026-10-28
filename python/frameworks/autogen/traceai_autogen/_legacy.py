"""ConversableAgent (pyautogen 0.2) hooks, kept separate from AgentChat."""

import functools
import inspect
import json

from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.trace import Status, StatusCode


def safe_json_dumps(value):
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def wrap_legacy(original, tracer, method):
    # Bind for observation only: never rewrite, drop or inject SDK arguments.
    signature = inspect.signature(original)

    def observe(agent, args, kwargs):
        try:
            bound = signature.bind(agent, *args, **kwargs).arguments
        except TypeError:
            # Let the SDK raise its own call error, with its original message.
            bound = {}
        extras = next((bound.get(name, {}) for name, parameter in signature.parameters.items()
                       if parameter.kind == inspect.Parameter.VAR_KEYWORD), {})
        if method.endswith("execute_function"):
            value = bound.get("func_call")
        elif method.endswith("initiate_chat"):
            value = bound.get("message", extras.get("message"))
        else:
            value = bound.get("messages")
        attributes = {
            SpanAttributes.GEN_AI_SPAN_KIND: "TOOL" if method.endswith("execute_function") else "AGENT",
            SpanAttributes.INPUT_VALUE: safe_json_dumps(value),
            SpanAttributes.INPUT_MIME_TYPE: "application/json",
            "agent.type": type(agent).__name__,
        }
        if method.endswith("execute_function"):
            name = value.get("name", "unknown") if isinstance(value, dict) else str(value)
            attributes[SpanAttributes.GEN_AI_TOOL_NAME] = name
        return attributes

    def finish(span, result):
        history = getattr(result, "chat_history", None)
        output = history[-1].get("content") if history else result
        span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(output))
        span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
        span.set_status(Status(StatusCode.OK))

    @functools.wraps(original)
    def sync_wrapper(self, *args, **kwargs):
        with tracer.start_as_current_span(
            f"autogen.{method}", attributes=observe(self, args, kwargs)
        ) as span:
            result = original(self, *args, **kwargs)
            finish(span, result)
            return result

    @functools.wraps(original)
    async def async_wrapper(self, *args, **kwargs):
        with tracer.start_as_current_span(
            f"autogen.{method}", attributes=observe(self, args, kwargs)
        ) as span:
            result = await original(self, *args, **kwargs)
            finish(span, result)
            return result

    return async_wrapper if inspect.iscoroutinefunction(original) else sync_wrapper
