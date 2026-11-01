"""Project native Strands spans at the export boundary, without creating spans."""

import json
import logging
from collections.abc import Sequence

from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status, StatusCode

logger = logging.getLogger(__name__)
_SCOPES = frozenset({"strands.telemetry.tracer", "traceai_strands"})
_METADATA = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.system",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.response.id",
        "gen_ai.agent.name",
        "gen_ai.agent.id",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "gen_ai.tool.status",
        "gen_ai.conversation.id",
        "session.id",
        "user.id",
        "tags",
        "event_loop.cycle_id",
        "event_loop.parent_cycle_id",
        "strands.cancellation.type",
    }
)
_USAGE = {
    "gen_ai.usage.input_tokens": (
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.prompt_tokens",
    ),
    "gen_ai.usage.output_tokens": (
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.completion_tokens",
    ),
    "gen_ai.usage.total_tokens": ("gen_ai.usage.total_tokens",),
    "gen_ai.usage.cache_read_tokens": (
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.cache_read_input_tokens",
    ),
    "gen_ai.usage.cache_write_tokens": (
        "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai.usage.cache_write_input_tokens",
    ),
}
_MAX_CONTENT = 1_000_000


def _decode(value, *, require_json=False):
    if not isinstance(value, str) or len(value) > _MAX_CONTENT:
        return None
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        # SDK attribute limits can truncate JSON before this export boundary.
        # Do not turn an undecodable history into trusted text during redaction.
        return None if require_json else value


def _messages(span, direction, *, require_json=False):
    """Read both native event conventions and the newer attribute-only opt-in."""
    key = f"gen_ai.{direction}.messages"
    attrs = span.attributes or {}
    if key in attrs:
        return _decode(attrs[key], require_json=require_json)
    messages = []
    for event in span.events:
        values = event.attributes or {}
        if key in values:
            decoded = _decode(values[key], require_json=require_json)
            messages.extend(decoded if isinstance(decoded, list) else [decoded])
        elif (
            direction == "input"
            and event.name
            in {
                "gen_ai.system.message",
                "gen_ai.user.message",
                "gen_ai.assistant.message",
                "gen_ai.tool.message",
            }
            and "content" in values
        ):
            messages.append(
                {
                    "role": event.name.split(".")[1],
                    "content": _decode(values["content"], require_json=require_json),
                }
            )
        elif (
            direction == "output"
            and event.name == "gen_ai.choice"
            and "message" in values
        ):
            messages.append(
                {
                    "role": "tool" if _kind(span) == "TOOL" else "assistant",
                    "content": _decode(values["message"], require_json=require_json),
                }
            )
    # Legacy callbacks use prompt/completion instead of native events.
    legacy = attrs.get("gen_ai.prompt" if direction == "input" else "gen_ai.completion")
    return messages or _decode(legacy, require_json=require_json)


def _redact_tool_results(value):
    """Remove failed or unverifiable tool responses from decoded message history.

    Legacy content blocks retain toolResult.status; newer OTel parts discard it.
    Never infer success from the enclosing chat/cycle span: it may have recovered
    from a tool error. No cross-span state is needed, even across export batches.
    """
    if isinstance(value, list):
        return [
            clean
            for item in value
            if (clean := _redact_tool_results(item)) is not None
        ]
    if not isinstance(value, dict):
        return value
    if "toolResult" in value:
        result = value["toolResult"]
        if not isinstance(result, dict) or result.get("status") != "success":
            return None
    if value.get("type") == "tool_call_response" and value.get("status") != "success":
        return None
    if value.get("role") == "tool":
        # Opaque tool messages may contain error text without a status. Retain
        # only structured calls/results that can be checked below, not raw text.
        value = {
            "role": "tool",
            **{
                key: [
                    block
                    for block in blocks
                    if isinstance(block, dict)
                    and (
                        "toolResult" in block
                        or block.get("type") in {"tool_call", "tool_call_response"}
                    )
                ]
                for key in ("content", "parts")
                if isinstance(blocks := value.get(key), list)
            },
        }
    return {
        key: clean
        for key, item in value.items()
        if (clean := _redact_tool_results(item)) is not None
    }


def _kind(span):
    operation = (span.attributes or {}).get("gen_ai.operation.name", "")
    if operation == "chat":
        return "LLM"
    if operation in {"execute_tool", "tool.execute"}:
        return "TOOL"
    if operation in {"invoke_agent", "agent.invoke"}:
        return "AGENT"
    return "CHAIN"


def _content_allowed(config, direction):
    # Native content is a JSON blob, not individually addressable FI message
    # fields. Fail closed on granular flags rather than leaking another copy.
    flags = (
        ("hide_inputs", "hide_input_messages", "hide_input_text", "hide_input_images")
        if direction == "input"
        else (
            "hide_outputs",
            "hide_output_messages",
            "hide_output_text",
        )
    )
    return not any(getattr(config, flag) for flag in flags)


def _project(span, config, capture_content):
    source = span.attributes or {}
    kind = _kind(span)
    tool_status = source.get("gen_ai.tool.status", source.get("tool.status"))
    # Strands 1.0 marks a returned tool error only in tool.status, leaving the
    # OTel status OK. Normalize that signal before applying error privacy.
    status_code = (
        StatusCode.ERROR
        if kind == "TOOL" and tool_status == "error"
        else span.status.status_code
    )
    attrs = {key: value for key, value in source.items() if key in _METADATA}
    attrs["gen_ai.span.kind"] = kind
    if kind == "TOOL" and tool_status is not None:
        attrs["gen_ai.tool.status"] = tool_status
    # The native system tag identifies the framework, not the model provider.
    # Preserve an explicit gen_ai.provider.name; never guess a provider from a
    # model name (the same model can be hosted by many different providers).
    if attrs.get("gen_ai.system") == "strands-agents":
        del attrs["gen_ai.system"]
    # Agent, cycle and multi-agent spans contain aggregates of model usage.
    # Only model spans carry billable usage; no trace-wide state or subtraction.
    if kind == "LLM":
        for destination, aliases in _USAGE.items():
            for key in aliases:
                value = source.get(key)
                if type(value) is int and 0 <= value < 2**63:
                    attrs[destination] = value
                    break

    hide_errors = not capture_content or config.hide_error_details
    if capture_content:
        for direction in ("input", "output"):
            if (
                direction == "output"
                and hide_errors
                and (
                    status_code == StatusCode.ERROR
                    or (kind == "TOOL" and tool_status != "success")
                )
            ):
                continue
            if not _content_allowed(config, direction):
                continue
            redact_history = hide_errors and kind != "TOOL"
            messages = _messages(span, direction, require_json=redact_history)
            if redact_history:
                # A tool span carries arguments and a directly verifiable result;
                # other spans carry history, whose tool statuses must be checked.
                messages = _redact_tool_results(messages)
            if messages is not None:
                value = json.dumps(messages, ensure_ascii=False)
                if len(value) <= _MAX_CONTENT:
                    attrs[f"gen_ai.{direction}.messages"] = value
                    attrs[f"{direction}.value"] = value
                    attrs[f"{direction}.mime_type"] = "application/json"
            keys = (
                (
                    "gen_ai.tool.call.arguments",
                    "gen_ai.system_instructions",
                    "gen_ai.tool.parameters",
                )
                if direction == "input"
                else (
                    "gen_ai.tool.call.result",
                    "gen_ai.tool.result",
                )
            )
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and len(value) <= _MAX_CONTENT:
                    attrs[key] = value

    attrs = {
        key: masked
        for key, value in attrs.items()
        if (masked := config.mask(key, value)) is not None
    }
    events = []
    for event in span.events:
        # Native content events have been projected above. Never retain a second,
        # unmasked copy or arbitrary callback/event/link attributes.
        if event.name != "exception":
            continue
        allowed = {"exception.type", "exception.escaped"}
        if not hide_errors:
            allowed.update({"exception.message", "exception.stacktrace"})
        values = {
            key: masked
            for key, value in (event.attributes or {}).items()
            if key in allowed and (masked := config.mask(key, value)) is not None
        }
        events.append(Event("exception", values, event.timestamp))
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attrs,
        events=events,
        links=[Link(link.context) for link in span.links],
        kind=span.kind,
        status=Status(
            status_code,
            None
            if hide_errors
            else config.mask("exception.message", span.status.description),
        ),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class StrandsSpanExporter(SpanExporter):
    """Wrap one exporter with Strands semantic conversion and TraceConfig privacy.

    Install as ``register(span_exporter=StrandsSpanExporter(HTTPSpanExporter()),
    set_global_tracer_provider=True, ...)`` BEFORE constructing an Agent. Native
    spans retain their IDs, parents and timing; other instrumentation scopes pass
    through unchanged. Content is opt-in, and native spans are never mutated.
    The wrapped exporter is owned by the provider's processor as usual.
    """

    _traceai_strands_adapter = True

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        capture_content: bool = False,
        config: TraceConfig | None = None,
    ) -> None:
        if not isinstance(capture_content, bool):
            raise TypeError("capture_content must be a bool")
        if config is not None and not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        self.exporter = exporter
        self.capture_content = capture_content
        self.config = config if config is not None else TraceConfig()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        projected = []
        for span in spans:
            scope = span.instrumentation_scope
            if scope is None or scope.name not in _SCOPES:
                projected.append(span)
                continue
            try:
                projected.append(_project(span, self.config, self.capture_content))
            except Exception as error:  # noqa: BLE001 - privacy boundary must fail closed
                # Do not log native content/tracebacks or export the raw fallback.
                logger.warning(
                    "Dropping an invalid Strands span (%s)", type(error).__name__
                )
        return self.exporter.export(projected)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.exporter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self.exporter.shutdown()
