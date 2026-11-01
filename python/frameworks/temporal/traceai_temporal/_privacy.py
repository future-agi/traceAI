"""Destination-local, fail-closed filtering of completed native spans."""

import logging
import re
from collections.abc import Sequence

from fi_instrumentation import TraceConfig
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext, Status, StatusCode

_LOGGER = logging.getLogger(__name__)

# An allowlist, not a prefix denylist: newly added upstream fields stay private.
_METADATA = frozenset(
    {
        "temporalWorkflowID",
        "temporalRunID",
        "temporalActivityID",
        "temporalActivityType",
        "session.id",
        "user.id",
        "gen_ai.conversation.id",
        "gen_ai.span.kind",
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.system",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.response.id",
        "gen_ai.tool.name",
        "gen_ai.tool.type",
        "gen_ai.tool.call.id",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.total_tokens",
        "gen_ai.usage.cache_read_tokens",
        "gen_ai.usage.cache_write_tokens",
        "gen_ai.usage.input_tokens.cache_read",
        "gen_ai.usage.input_tokens.cache_write",
        "gen_ai.usage.input_tokens.cache_input",
        "gen_ai.usage.input_tokens.audio",
        "gen_ai.usage.output_tokens.audio",
        "gen_ai.usage.output_tokens.reasoning",
        "llm.provider",
        "llm.system",
        "tool.name",
        "tool.id",
        "tool.type",
        "exception.type",
        "error.type",
    }
)
_ALIASES = {
    "fi.span.kind": "gen_ai.span.kind",
    "openinference.span.kind": "gen_ai.span.kind",
    "llm.model_name": "gen_ai.request.model",
    "llm.invocation_parameters": "gen_ai.request.parameters",
    "llm.token_count.prompt": "gen_ai.usage.input_tokens",
    "llm.token_count.completion": "gen_ai.usage.output_tokens",
    "llm.token_count.total": "gen_ai.usage.total_tokens",
}
_CONTENT = frozenset(
    {
        "input.value",
        "output.value",
        "input.mime_type",
        "output.mime_type",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.request.parameters",
        "raw.input",
        "raw.output",
        "gen_ai.prompts",
    }
)
_MESSAGE_FIELD = re.compile(
    r"(?:gen_ai\.(?:input|output)\.messages|llm\.(?:input|output)_messages)"
    r"\.\d+\.message\.(?:role|content|name|tool_call_id|"
    r"contents\.\d+\.message_content\.text|"
    r"tool_calls\.\d+\.tool_call\.(?:id|function\.name|function\.arguments))\Z"
)

_RESOURCE = frozenset(
    {"project_name", "project_type", "service.name", "semantic_convention"}
)
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,255}\Z")
_KINDS = frozenset(
    {
        "llm",
        "tool",
        "chain",
        "agent",
        "retriever",
        "embedding",
        "reranker",
        "guardrail",
        "evaluator",
    }
)


def _context(context):
    if context is None:
        return None
    # TraceState is arbitrary vendor data; it is unnecessary for exported parentage.
    return SpanContext(
        context.trace_id, context.span_id, context.is_remote, context.trace_flags
    )


class SafeSpanExporter(SpanExporter):
    """Sanitize only this destination; never mutate spans seen by other exporters.

    Content requires capture_content. Exception messages additionally require
    keep_error_text; exception stacks and arbitrary events are never exported.
    Span/scope names and metadata IDs must not contain private application data.
    The wrapped exporter is owned by this wrapper and shut down with it.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        capture_content: bool = False,
        keep_error_text: bool = False,
        max_content_length: int = 16384,
    ) -> None:
        if not isinstance(capture_content, bool) or not isinstance(
            keep_error_text, bool
        ):
            raise TypeError("capture_content and keep_error_text must be booleans")
        if not isinstance(max_content_length, int) or isinstance(
            max_content_length, bool
        ):
            raise TypeError("max_content_length must be an integer")
        if max_content_length < 1:
            raise ValueError("max_content_length must be positive")
        self._exporter = exporter
        self._capture_content = capture_content
        self._config = TraceConfig()
        self._keep_error_text = (
            capture_content and keep_error_text and not self._config.hide_error_details
        )
        self._max_content_length = max_content_length

    def _attributes(self, attributes):
        original = dict(attributes or {})
        for source, target in _ALIASES.items():
            if source in original:
                original.setdefault(target, original[source])
                del original[source]
        result = {}
        for key, value in original.items():
            if key in {"exception.type", "error.type"}:
                result[key] = (
                    value
                    if isinstance(value, str) and _ERROR_TYPE.fullmatch(value)
                    else "Error"
                )
            elif key == "gen_ai.span.kind":
                if isinstance(value, str) and value.lower() in _KINDS:
                    result[key] = value.lower()
            elif key.startswith("gen_ai.usage.") and key in _METADATA:
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    result[key] = value
            elif key in _METADATA:
                if isinstance(value, (str, int, float, bool)):
                    result[key] = value
            elif self._keep_error_text and key in {
                "exception.message",
                "error.message",
            }:
                if isinstance(value, str):
                    result[key] = value[: self._max_content_length]
            elif self._capture_content and (
                key in _CONTENT or _MESSAGE_FIELD.fullmatch(key)
            ):
                # Aggregate strings may contain message text/images that core's
                # field-level mask cannot inspect. Hide them conservatively.
                input_hidden = any(
                    (
                        self._config.hide_inputs,
                        self._config.hide_input_messages,
                        self._config.hide_input_text,
                        self._config.hide_input_images,
                    )
                )
                output_hidden = any(
                    (
                        self._config.hide_outputs,
                        self._config.hide_output_messages,
                        self._config.hide_output_text,
                    )
                )
                is_input = key.startswith(
                    ("input.", "raw.input", "gen_ai.input.", "llm.input_messages")
                ) or key in {
                    "gen_ai.system_instructions",
                    "gen_ai.prompts",
                    "gen_ai.tool.call.arguments",
                }
                is_output = (
                    key.startswith(
                        (
                            "output.",
                            "raw.output",
                            "gen_ai.output.",
                            "llm.output_messages",
                        )
                    )
                    or key == "gen_ai.tool.call.result"
                )
                if (is_input and input_hidden) or (is_output and output_hidden):
                    continue
                if key == "gen_ai.request.parameters" and (
                    input_hidden
                    or output_hidden
                    or self._config.hide_llm_invocation_parameters
                ):
                    continue
                masked = self._config.mask(key, value)
                if masked is not None:
                    if isinstance(masked, str):
                        result[key] = masked[: self._max_content_length]
                    elif isinstance(masked, (int, float, bool)):
                        result[key] = masked
        return result

    def _scrub(self, span: ReadableSpan) -> ReadableSpan:
        status = Status(span.status.status_code)
        if span.status.status_code == StatusCode.ERROR:
            status = Status(
                StatusCode.ERROR,
                (
                    span.status.description[: self._max_content_length]
                    if self._keep_error_text and span.status.description
                    else "error"
                ),
            )
        events = []
        for event in span.events:
            if event.name != "exception":
                continue
            attrs = event.attributes or {}
            safe = self._attributes(
                {k: v for k, v in attrs.items() if k == "exception.type"}
            )
            message = attrs.get("exception.message")
            if self._keep_error_text and isinstance(message, str):
                safe["exception.message"] = message[: self._max_content_length]
            events.append(Event("exception", safe, event.timestamp))
        scope = span.instrumentation_scope
        return ReadableSpan(
            name=span.name,
            context=_context(span.context),
            parent=_context(span.parent),
            resource=Resource(
                {k: v for k, v in span.resource.attributes.items() if k in _RESOURCE}
            ),
            attributes=self._attributes(span.attributes),
            events=events,
            links=[Link(_context(link.context)) for link in span.links],
            kind=span.kind,
            status=status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=InstrumentationScope(scope.name, scope.version)
            if scope
            else None,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            safe = [self._scrub(span) for span in spans]
            return self._exporter.export(safe)
        except Exception:  # noqa: BLE001 -- fail closed for both filter and exporter failures
            # Neither a filtering error nor an exporter exception may send the originals
            # or log exception text that can contain a request body/credentials.
            _LOGGER.warning(
                "Temporal span filtering or export failed; batch dropped, details redacted"
            )
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._exporter.shutdown()
