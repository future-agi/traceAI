"""Shared fixtures for traceai-ag2 tests: a scripted AG2 model and span helpers.

The model is AG2's own ``ag2.testing.TestConfig``: no network, no API key.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

USER_PROMPT = "What's the weather in Paris?"
FINAL_ANSWER = "It is sunny in Paris."
TOOL_RESULT = "sunny in Paris"
TOOL_CALL_ID = "call_weather_1"
MODEL = "fake-model"
PROVIDER = "fake-provider"

# Every attribute AG2 sets only when capture_content=True, plus any key the
# processor would have to drop for TraceConfig.
CONTENT_KEYS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "ag2.human_input.prompt",
    "ag2.human_input.response",
)
CONTENT_STRINGS = (USER_PROMPT, FINAL_ANSWER, TOOL_RESULT, '"city"')


def emits_record_usage() -> bool:
    """``record_usage {kind}`` spans exist from ag2 1.0.3 (telemetry.py:420 at 1.1.2)."""
    from ag2.middleware.builtin import telemetry

    return hasattr(telemetry._TelemetryMiddlewareInstance, "record_usage_span")


def expected_one_tool_kinds() -> Dict[str, Optional[str]]:
    """Span name -> expected ``gen_ai.span.kind`` for the weather run."""
    kinds: Dict[str, Optional[str]] = {
        "invoke_agent weather_bot": "AGENT",
        "chat": "LLM",
        "execute_tool get_weather": "TOOL",
        f"chat {MODEL}": "LLM",
    }
    if emits_record_usage():
        kinds["record_usage model_call"] = None
    return kinds


def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"sunny in {city}"


def weather_config(*, with_provider: bool = True) -> Any:
    """One tool call, then a final answer carrying every usage field AG2 reads."""
    from ag2.events import ModelMessage, ModelResponse, ToolCallEvent
    from ag2.testing import TestConfig
    from ag2.usage import Usage

    final_kwargs: Dict[str, Any] = {
        "usage": Usage(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=2,
            thinking_tokens=5,
        ),
        "model": MODEL,
        "finish_reason": "stop",
    }
    if with_provider:
        final_kwargs["provider"] = PROVIDER
    return TestConfig(
        ToolCallEvent("get_weather", arguments='{"city": "Paris"}', id=TOOL_CALL_ID),
        ModelResponse(ModelMessage(FINAL_ANSWER), **final_kwargs),
    )


def weather_agent(name: str = "weather_bot", *, with_provider: bool = True) -> Any:
    from ag2 import Agent

    return Agent(
        name,
        "You answer weather questions.",
        config=weather_config(with_provider=with_provider),
        tools=[get_weather],
    )


def ask(agent: Any, prompt: str = USER_PROMPT, **kwargs: Any) -> Any:
    async def _run() -> Any:
        return await agent.ask(prompt, **kwargs)

    return asyncio.run(_run())


def memory_provider() -> "tuple[TracerProvider, InMemorySpanExporter]":
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})


def by_name(spans: List[ReadableSpan]) -> Dict[str, ReadableSpan]:
    out: Dict[str, ReadableSpan] = {}
    for span in spans:
        assert span.name not in out, f"duplicate span name {span.name!r}"
        out[span.name] = span
    return out


def find(spans: List[ReadableSpan], prefix: str) -> List[ReadableSpan]:
    return [s for s in spans if s.name.startswith(prefix)]


def otlp_value(value: Dict[str, Any]) -> Any:
    """Decode one OTLP JSON ``AnyValue`` (as MessageToDict renders it)."""
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [otlp_value(v) for v in value["arrayValue"].get("values", [])]
    return value


def otlp_attrs(span: Dict[str, Any]) -> Dict[str, Any]:
    return {a["key"]: otlp_value(a.get("value", {})) for a in span.get("attributes", [])}


def all_string_values(attributes: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for value in attributes.values():
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(str(v) for v in value)
    return out


def assert_no_content(attributes: Dict[str, Any], where: Optional[str] = None) -> None:
    for key in CONTENT_KEYS:
        assert key not in attributes, f"{key} present on {where}"
    for text in all_string_values(attributes):
        for needle in CONTENT_STRINGS:
            assert needle not in text, f"content {needle!r} leaked on {where}: {text!r}"
