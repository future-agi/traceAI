"""Destination-local projection of native spans; never start or mutate a span."""

import logging
from collections.abc import Mapping, Sequence

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext, Status

RENAMES = {
    "oi_span_type": "fi.span.kind",
    "conversation_id": "session.id",
    "user_id": "user.id",
    "input": "input.value",
    "output": "output.value",
}
METADATA = frozenset(
    {
        "fi.span.kind",
        "session.id",
        "user.id",
        "id",
        "name",
        "type",
        "model",
        "invoke_id",
        "assistant_request_id",
        "subscribed_topics",
        "publish_to_topics",
        "error.type",
        "openinference.span.kind",
        "llm.model_name",
        "llm.provider",
        "llm.system",
        "llm.token_count.prompt",
        "llm.token_count.completion",
        "llm.token_count.total",
    }
)
CONTENT = frozenset(
    {"input.value", "output.value", "input.mime_type", "output.mime_type"}
)
# Free-form kwargs/error blobs and unknown fields stay private in both modes.
MAX_CONTENT_CHARS = 65536


def _context(context):
    if context is None:
        return None
    return SpanContext(
        context.trace_id, context.span_id, context.is_remote, context.trace_flags
    )


def _attributes(
    attributes: Mapping | None, capture_content: bool, capture_errors: bool
) -> dict:
    result = {}
    for key, value in (attributes or {}).items():
        key = RENAMES.get(key, key)
        if isinstance(value, Mapping) or value is None:
            continue
        if key in {"session.id", "user.id"} and value == "":
            continue
        if key in METADATA or (
            (
                (capture_content and key in CONTENT)
                or (capture_errors and key == "error.message")
            )
            and isinstance(value, str)
            and len(value) <= MAX_CONTENT_CHARS
        ):
            result[key] = value
    return result


def _project(
    span: ReadableSpan, capture_content: bool, capture_errors: bool
) -> ReadableSpan:
    attrs = _attributes(span.attributes, capture_content, capture_errors)
    events = []
    for event in span.events:
        # Unknown event names can contain user content, not just their attributes.
        if event.name != "exception":
            continue
        allowed = {"exception.type"}
        if capture_errors:
            allowed.add("exception.message")
        event_attrs = {
            key: value
            for key, value in (event.attributes or {}).items()
            if key in allowed
            and isinstance(value, str)
            and len(value) <= MAX_CONTENT_CHARS
        }
        events.append(Event("exception", event_attrs, event.timestamp))
    resource = {
        key: value
        for key, value in span.resource.attributes.items()
        if key in {"project_name", "project_type", "service.name"}
    }
    # Context/parent/link IDs are unchanged; arbitrary tracestate and link, scope,
    # resource attributes are not a content escape hatch.
    scope = span.instrumentation_scope
    description = span.status.description if capture_errors else None
    if description and len(description) > MAX_CONTENT_CHARS:
        description = None
    return ReadableSpan(
        name=span.name,
        context=_context(span.context),
        parent=_context(span.parent),
        kind=span.kind,
        resource=Resource(resource),
        attributes=attrs,
        events=events,
        links=[Link(_context(link.context)) for link in span.links],
        status=Status(span.status.status_code, description),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=InstrumentationScope(scope.name, scope.version)
        if scope
        else None,
    )


class GrafiExporter(SpanExporter):
    """Map and filter the dedicated provider's export without altering native spans.

    Content and error messages are separate opt-ins. Stack fields and unknown
    attributes are never exported. Names and metadata IDs are application-owned:
    use static component names and opaque identifiers, not user content.
    """

    def __init__(
        self,
        inner: SpanExporter,
        capture_content: bool = False,
        *,
        capture_errors: bool = False,
    ):
        self._inner = inner
        self._capture_content = capture_content
        self._capture_errors = capture_errors

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        # Projection failure is fail-closed: never fall back to the original batch.
        try:
            mapped = [
                _project(span, self._capture_content, self._capture_errors)
                for span in spans
            ]
        except Exception:  # noqa: BLE001 - privacy boundary must fail closed
            # Do not log the exception or raw span: it can contain private content.
            logging.getLogger(__name__).warning(
                "Graphite span projection failed; batch not exported"
            )
            return SpanExportResult.FAILURE
        return self._inner.export(mapped)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._inner.shutdown()
