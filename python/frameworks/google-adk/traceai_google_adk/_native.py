"""Enrich ADK-owned spans without replacing its execution or stream lifecycle."""

import json
from typing import Any, Mapping, Sequence

import wrapt
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.util.types import Attributes, AttributeValue

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes as SA

# Do not export arbitrary ADK attributes: legacy payloads, state, and future
# experimental attributes can contain content outside TraceConfig's namespace.
_METADATA = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.response.id",
        "gen_ai.response.finish_reasons",
        "gen_ai.agent.name",
        "gen_ai.agent.id",
        "gen_ai.conversation.id",
        "gen_ai.tool.name",
        "gen_ai.tool.type",
        "gen_ai.tool.call.id",
        "gen_ai.request.temperature",
        "gen_ai.request.top_p",
        "gen_ai.request.top_k",
        "gen_ai.request.max_tokens",
        "gen_ai.span.kind",
        "session.id",
        "user.id",
        "error.type",
    }
)
_CONTENT = {
    "gen_ai.input.messages": SA.GEN_AI_INPUT_MESSAGES,
    "gen_ai.output.messages": SA.GEN_AI_OUTPUT_MESSAGES,
    "gen_ai.system_instructions": SA.GEN_AI_SYSTEM_INSTRUCTIONS,
    "gen_ai.tool.call.arguments": SA.INPUT_VALUE,
    "gen_ai.tool.call.result": SA.OUTPUT_VALUE,
    "gcp.vertex.agent.tool_call_args": SA.INPUT_VALUE,
    "gcp.vertex.agent.tool_response": SA.OUTPUT_VALUE,
}
_USAGE = {
    "prompt_token_count": SA.GEN_AI_USAGE_INPUT_TOKENS,
    "candidates_token_count": SA.GEN_AI_USAGE_OUTPUT_TOKENS,
    "total_token_count": SA.GEN_AI_USAGE_TOTAL_TOKENS,
    "cached_content_token_count": SA.GEN_AI_USAGE_CACHE_READ_TOKENS,
    "thoughts_token_count": SA.GEN_AI_USAGE_OUTPUT_TOKENS_REASONING,
}


def _kind(name: str) -> str:
    operation = name.split(" ", 1)[0]
    if operation in {"call_llm", "generate_content", "chat", "inference"}:
        return "LLM"
    if operation == "execute_tool" and name not in {
        "execute_tool (merged)",
        "execute_tool (merged tools)",
    }:
        return "TOOL"
    if operation in {"invoke_agent", "agent_run"}:
        return "AGENT"
    return "CHAIN"


class _Attributes:
    def __init__(self, config: TraceConfig) -> None:
        # Aggregated native JSON cannot safely implement per-part masking.
        # Fine-grained hiding therefore conservatively hides that direction.
        self.hide_input = any(
            (
                config.hide_inputs,
                config.hide_input_messages,
                config.hide_input_text,
                config.hide_input_images,
            )
        )
        self.hide_output = any(
            (config.hide_outputs, config.hide_output_messages, config.hide_output_text)
        )
        self.hide_parameters = config.hide_llm_invocation_parameters

    def map(self, key: str, value: AttributeValue) -> dict[str, AttributeValue]:
        if key in _METADATA:
            return {key: value}
        if key.startswith("gen_ai.usage.") and isinstance(value, (int, float)):
            return {key: value}
        if key == "gcp.vertex.agent.session_id":
            return {SA.GEN_AI_CONVERSATION_ID: value, SA.SESSION_ID: value}
        if key in _CONTENT:
            target = _CONTENT[key]
            output = target in {SA.OUTPUT_VALUE, SA.GEN_AI_OUTPUT_MESSAGES}
            if self.hide_output if output else self.hide_input:
                return {}
            mapped = {target: value}
            if target in {SA.GEN_AI_INPUT_MESSAGES, SA.GEN_AI_OUTPUT_MESSAGES}:
                mapped[SA.OUTPUT_VALUE if output else SA.INPUT_VALUE] = value
                mapped[SA.OUTPUT_MIME_TYPE if output else SA.INPUT_MIME_TYPE] = (
                    "application/json"
                )
            return mapped
        if key in {"gcp.vertex.agent.llm_request", "gcp.vertex.agent.llm_response"}:
            return self._legacy_payload(key, value)
        return {}

    def _legacy_payload(
        self, key: str, value: AttributeValue
    ) -> dict[str, AttributeValue]:
        if not isinstance(value, str):
            return {}
        try:
            data = json.loads(value)
        except ValueError:
            # Drop malformed native payloads, without logging private content.
            return {}
        if not isinstance(data, dict):
            return {}
        attrs: dict[str, AttributeValue] = {}
        if key.endswith("llm_response"):
            usage = data.get("usage_metadata")
            if isinstance(usage, dict):
                for source, target in _USAGE.items():
                    count = usage.get(source)
                    if isinstance(count, int) and not isinstance(count, bool):
                        attrs[target] = count
            if not self.hide_output and isinstance(data.get("content"), dict):
                content = json.dumps(data["content"])
                attrs.update(
                    {
                        SA.OUTPUT_VALUE: content,
                        SA.OUTPUT_MIME_TYPE: "application/json",
                        SA.GEN_AI_OUTPUT_MESSAGES: json.dumps([data["content"]]),
                    }
                )
        else:
            if isinstance(data.get("model"), str):
                attrs[SA.GEN_AI_REQUEST_MODEL] = data["model"]
            if not self.hide_input:
                # Never forward raw config: HTTP options can contain credentials.
                if isinstance(data.get("contents"), list):
                    content = json.dumps(data["contents"])
                    attrs.update(
                        {
                            SA.INPUT_VALUE: content,
                            SA.INPUT_MIME_TYPE: "application/json",
                            SA.GEN_AI_INPUT_MESSAGES: content,
                        }
                    )
                config = data.get("config")
                if isinstance(config, dict):
                    if "system_instruction" in config:
                        attrs[SA.GEN_AI_SYSTEM_INSTRUCTIONS] = json.dumps(
                            config["system_instruction"]
                        )
                    if not self.hide_parameters:
                        for source, target in (
                            ("temperature", "gen_ai.request.temperature"),
                            ("top_p", "gen_ai.request.top_p"),
                            ("max_output_tokens", "gen_ai.request.max_tokens"),
                        ):
                            if isinstance(config.get(source), (int, float)):
                                attrs[target] = config[source]
        return attrs

    def all(
        self, attributes: Mapping[str, AttributeValue] | None
    ) -> dict[str, AttributeValue]:
        result: dict[str, AttributeValue] = {}
        for key, value in (attributes or {}).items():
            result.update(self.map(key, value))
        return result


class _NativeSpan(wrapt.ObjectProxy):
    def __init__(
        self, span: trace.Span, mapper: _Attributes, name: str, kind: str
    ) -> None:
        super().__init__(span)
        self._self_mapper = mapper
        self._self_name = name
        self._self_kind = kind

    def _demote_model_container(self) -> None:
        # Legacy ADK may nest generate_content under call_llm. Keep its native
        # topology, but account for the model call and tokens only on the leaf.
        self._self_kind = "CHAIN"
        self.__wrapped__.set_attribute(SA.GEN_AI_SPAN_KIND, "CHAIN")

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, attributes: Mapping[str, AttributeValue]) -> None:
        mapped = self._self_mapper.all(attributes)
        if self._self_kind != "LLM":
            mapped = {
                key: value
                for key, value in mapped.items()
                if not key.startswith("gen_ai.usage.")
            }
        self.__wrapped__.set_attributes(mapped)

    def add_event(
        self, name: str, attributes: Attributes = None, timestamp: int | None = None
    ) -> None:
        # ADK's arbitrary events are not a second content channel. Exception
        # attributes/status still pass through FI's dedicated error redaction.
        if name == "exception":
            self.__wrapped__.add_event(
                name,
                attributes=self._exception_attributes(attributes),
                timestamp=timestamp,
            )

    def _exception_attributes(
        self, attributes: Attributes
    ) -> dict[str, AttributeValue]:
        mapped = self._self_mapper.all(attributes)
        mapped.update(
            {
                key: value
                for key, value in (attributes or {}).items()
                if key
                in {
                    "exception.type",
                    "exception.message",
                    "exception.stacktrace",
                    "exception.escaped",
                }
            }
        )
        return mapped

    def record_exception(
        self,
        exception: BaseException,
        attributes: Attributes = None,
        timestamp: int | None = None,
        escaped: bool = False,
    ) -> None:
        self.__wrapped__.record_exception(
            exception,
            attributes=self._exception_attributes(attributes),
            timestamp=timestamp,
            escaped=escaped,
        )

    def add_link(
        self, context: trace.SpanContext, attributes: Attributes = None
    ) -> None:
        self.__wrapped__.add_link(context, attributes=self._self_mapper.all(attributes))

    def __enter__(self) -> "_NativeSpan":
        return self

    def __exit__(self, *args: Any) -> Any:
        return self.__wrapped__.__exit__(*args)


class NativeTracer(FITracer):
    def __init__(self, tracer: trace.Tracer, config: TraceConfig) -> None:
        super().__init__(tracer, config=config)
        self._self_mapper = _Attributes(config)

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
        **kwargs: Any,
    ) -> _NativeSpan:
        mapped = self._self_mapper.all(attributes)
        logical_kind = _kind(name)
        mapped[SA.GEN_AI_SPAN_KIND] = logical_kind
        parent = trace.get_current_span(context)
        if mapped[SA.GEN_AI_SPAN_KIND] != "LLM":
            mapped = {
                key: value
                for key, value in mapped.items()
                if not key.startswith("gen_ai.usage.")
            }
        # Legacy native ADK has only the agent name in the span name.
        if mapped[SA.GEN_AI_SPAN_KIND] == "AGENT" and " " in name:
            mapped.setdefault(SA.GEN_AI_AGENT_NAME, name.split(" ", 1)[1].strip("[]"))
        if mapped[SA.GEN_AI_SPAN_KIND] == "TOOL" and " " in name:
            mapped.setdefault(SA.GEN_AI_TOOL_NAME, name.split(" ", 1)[1])
        safe_links = (
            None
            if links is None
            else [
                trace.Link(link.context, self._self_mapper.all(link.attributes))
                for link in links
            ]
        )
        span = super().start_span(
            name=name,
            context=context,
            kind=kind,
            attributes=mapped,
            links=safe_links,
            start_time=start_time,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
            **kwargs,
        )
        if (
            span.is_recording()
            and mapped[SA.GEN_AI_SPAN_KIND] == "LLM"
            and name != "call_llm"
            and isinstance(parent, _NativeSpan)
            and parent._self_name == "call_llm"
        ):
            parent._demote_model_container()
        return _NativeSpan(span, self._self_mapper, name, logical_kind)
