"""Project native Pydantic AI attributes before the SDK sees them.

The framework owns span lifetimes, streaming and exceptions. This adapter only
adds Future AGI semantics and filters native payload fields. It never registers
processors on, mutates, or shuts down the application's tracer provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from fi_instrumentation.instrumentation import TraceConfig
from fi_instrumentation.instrumentation._tracers import FITracer
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.util.types import Attributes, AttributeValue

_KIND = "gen_ai.span.kind"
_USAGE = "gen_ai.usage."
_AGGREGATED = "gen_ai.aggregated_usage."
# Unknown attributes, schemas, native metadata and logfire messages are omitted.
_SAFE = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.system",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.response.id",
        "gen_ai.agent.name",
        "gen_ai.agent.call.id",
        "gen_ai.conversation.id",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "gen_ai.tool.type",
    }
)
_TOKENS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read.input_tokens",
        "cache_creation.input_tokens",
        "reasoning.output_tokens",
        "details.reasoning_tokens",
        "details.output_reasoning_tokens",
    }
)
_ALIASES = {
    "model_name": "gen_ai.request.model",
    "agent_name": "gen_ai.agent.name",
    "tool_name": "gen_ai.tool.name",
    "gen_ai.usage.details.cache_read_tokens": "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.details.cache_write_tokens": "gen_ai.usage.cache_creation.input_tokens",
}
_CONTENT = {
    "gen_ai.input.messages": ("gen_ai.input.messages", "input.value"),
    "gen_ai.output.messages": ("gen_ai.output.messages", "output.value"),
    "gen_ai.system_instructions": ("gen_ai.system_instructions",),
    "tool_arguments": ("gen_ai.tool.call.arguments", "input.value"),
    "gen_ai.tool.call.arguments": ("gen_ai.tool.call.arguments", "input.value"),
    "tool_response": ("gen_ai.tool.call.result", "output.value"),
    "gen_ai.tool.call.result": ("gen_ai.tool.call.result", "output.value"),
    "final_result": ("output.value",),
}


def span_kind(name: str, attributes: Attributes) -> str:
    attrs = attributes or {}
    operation = attrs.get("gen_ai.operation.name")
    if operation == "invoke_agent" or name == "agent run":
        return "AGENT"
    if operation == "chat":
        return "LLM"
    if (
        operation == "execute_tool"
        or "gen_ai.tool.name" in attrs
        or "tool_name" in attrs
    ):
        return "TOOL"
    return "CHAIN"


def project(
    attributes: Attributes, kind: str, include_content: bool
) -> dict[str, AttributeValue]:
    result = {}
    for key, value in (attributes or {}).items():
        key = _ALIASES.get(key, key)
        if key in _SAFE:
            result[key] = value
        elif kind == "LLM" and key.startswith(_USAGE) and key[len(_USAGE) :] in _TOKENS:
            if type(value) is int and value >= 0:
                result[key] = value
        elif include_content and key in _CONTENT:
            for target in _CONTENT[key]:
                result[target] = value
                if target in ("input.value", "output.value"):
                    try:
                        json.loads(value)
                        mime_type = "application/json"
                    except (TypeError, ValueError):
                        mime_type = "text/plain"
                    result[target.replace("value", "mime_type")] = mime_type
    return result


class NativeTracerProvider(trace.TracerProvider):
    def __init__(
        self, provider: trace.TracerProvider, config: TraceConfig, include_content: bool
    ) -> None:
        self.provider = provider
        self.config = config
        self.include_content = include_content
        self.enabled = True

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> trace.Tracer:
        tracer = self.provider.get_tracer(
            instrumenting_module_name,
            instrumenting_library_version,
            schema_url=schema_url,
            attributes=attributes,
        )
        return NativeTracer(FITracer(tracer, self.config), self)


class NativeTracer(trace.Tracer):
    def __init__(self, tracer: trace.Tracer, provider: NativeTracerProvider) -> None:
        self.tracer = tracer
        self.provider = provider

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: trace.SpanKind = trace.SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[trace.Link] | None = (),
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> trace.Span:
        if not self.provider.enabled:
            return trace.INVALID_SPAN
        logical_kind = span_kind(name, attributes)
        attrs = project(attributes, logical_kind, self.provider.include_content)
        attrs[_KIND] = logical_kind
        # Names in native internal spans can contain payloads. Retain only the
        # supported operation and identifier names, never logfire's formatted msg.
        operation = {
            "AGENT": "invoke_agent",
            "LLM": "chat",
            "TOOL": "execute_tool",
        }.get(logical_kind)
        identifier = {
            "AGENT": "gen_ai.agent.name",
            "LLM": "gen_ai.request.model",
            "TOOL": "gen_ai.tool.name",
        }.get(logical_kind)
        safe_name = (
            f"{operation} {attrs.get(identifier, '')}".strip()
            if operation
            else "pydantic_ai.step"
        )
        span = self.tracer.start_span(
            safe_name,
            context=context,
            kind=kind,
            attributes=attrs,
            links=[trace.Link(link.context) for link in links] if links else links,
            start_time=start_time,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        )
        parent = trace.get_current_span(context)
        agent = (
            (parent if parent.logical_kind == "AGENT" else parent.agent)
            if isinstance(parent, NativeSpan)
            else None
        )
        return NativeSpan(
            span, logical_kind, self.provider.include_content, agent, attrs
        )

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Context | None = None,
        kind: trace.SpanKind = trace.SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[trace.Link] | None = (),
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[trace.Span]:
        span = self.start_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
        )
        with trace.use_span(
            span,
            end_on_exit=end_on_exit,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        ):
            yield span


class NativeSpan(trace.Span):
    def __init__(
        self,
        span: trace.Span,
        logical_kind: str,
        include_content: bool,
        agent: NativeSpan | None,
        attributes: Attributes,
    ) -> None:
        self.span = span
        self.logical_kind = logical_kind
        self.include_content = include_content
        self.agent = agent
        self.tokens: dict[str, int] = {
            key[len(_USAGE) :]: value
            for key, value in (attributes or {}).items()
            if key.startswith(_USAGE) and type(value) is int
        }
        self.ended = False

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        if self.ended:
            return
        attrs = project({key: value}, self.logical_kind, self.include_content)
        for mapped, val in attrs.items():
            if mapped.startswith(_USAGE):
                self.tokens[mapped[len(_USAGE) :]] = val
            self.span.set_attribute(mapped, val)
            if self.logical_kind == "LLM" and mapped == "gen_ai.request.model":
                self.span.update_name(f"chat {val}")

    def set_attributes(self, attributes: Attributes) -> None:
        for key, value in (attributes or {}).items():
            self.set_attribute(key, value)

    def end(self, end_time: int | None = None) -> None:
        if self.ended:
            return
        self.ended = True
        if self.logical_kind == "LLM" and self.is_recording():
            if "input_tokens" in self.tokens and "output_tokens" in self.tokens:
                self.span.set_attribute(
                    _USAGE + "total_tokens",
                    self.tokens["input_tokens"] + self.tokens["output_tokens"],
                )
            if self.agent is not None:
                for key, value in self.tokens.items():
                    self.agent.tokens[key] = self.agent.tokens.get(key, 0) + value
        elif self.logical_kind == "AGENT":
            # Sum only this run's model requests. Native older releases report
            # shared/cumulative RunUsage, which may include earlier agent runs.
            for key, value in self.tokens.items():
                self.span.set_attribute(_AGGREGATED + key, value)
            if "input_tokens" in self.tokens and "output_tokens" in self.tokens:
                self.span.set_attribute(
                    _AGGREGATED + "total_tokens",
                    self.tokens["input_tokens"] + self.tokens["output_tokens"],
                )
        self.span.end(end_time)

    def get_span_context(self) -> trace.SpanContext:
        return self.span.get_span_context()

    def is_recording(self) -> bool:
        return self.span.is_recording()

    def set_status(
        self, status: trace.Status | trace.StatusCode, description: str | None = None
    ) -> None:
        self.span.set_status(status, description)

    def record_exception(
        self,
        exception: BaseException,
        attributes: Attributes = None,
        timestamp: int | None = None,
        escaped: bool = False,
    ) -> None:
        self.span.record_exception(
            exception,
            attributes=project(attributes, self.logical_kind, self.include_content),
            timestamp=timestamp,
            escaped=escaped,
        )

    def add_event(
        self, name: str, attributes: Attributes = None, timestamp: int | None = None
    ) -> None:
        # Arbitrary native events can quote payloads even with content disabled.
        if name == "exception":
            self.span.add_event(name, {}, timestamp)

    def update_name(self, name: str) -> None:
        # Native names were projected at creation; do not reintroduce raw names.
        return None

    def add_link(
        self, context: trace.SpanContext, attributes: Attributes = None
    ) -> None:
        self.span.add_link(context, {})
