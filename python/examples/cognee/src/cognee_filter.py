"""Export filter for Cognee's spans: content off by default, search and LLM kinds set.

Recipe code, not a package: copy this file next to your app. It wraps the
exporter that sends spans to Future AGI (see app.py)::

    exporter = CogneeExportFilter(HTTPSpanExporter())
    provider.add_span_processor(BatchSpanProcessor(exporter))

and changes only what that exporter sends. It exports copies, so Cognee's
own span buffer and any other processor still see the original spans.

Content. Cognee 1.6.5 writes prompts, document text, answers and search
questions to span attributes and has no setting to stop that, and its
errors can quote them (a content-policy rejection quotes the whole prompt).
By default the filter removes those attributes (``CONTENT_KEYS``) and, on
every span, all free-text error detail: exception messages and status
descriptions are replaced, exception stack traces are omitted, and other event
values are kept only if they are not text. Pass ``capture_content=True``, or set
``COGNEE_FI_CAPTURE_CONTENT=true``, to export content and error messages too.
With capture on, the filter also copies that content to ``input.value`` and
``output.value`` (``IO_SOURCES``), the keys Future AGI shows as a span's Input
and Output.

Stack traces. ``exception.stacktrace`` is never exported, with or without
content capture: a serialized traceback carries local file paths (home
directory, user name, virtualenv layout) and can quote content.

Span kinds. Cognee sets no span-kind attribute, so Future AGI would store
search and LLM spans as ``unknown``. The filter sets ``fi.span.kind`` by span
name (``span_kind``), only where none of ``SPAN_KIND_KEYS`` is set.

Missing collections. In every search Cognee probes vector collections that
do not exist yet, and each probe's ``cognee.db.vector.search`` span ends in
ERROR with ``CollectionNotFoundError``, so every successful trace would show
dozens of errors. By default the filter exports a vector-search span whose
only error is that one as OK, without its exception event, and with
``cognee.vector_search.collection_missing=true``. Every other error stays
ERROR. Pass ``collection_missing_as_error=True``, or set
``COGNEE_FI_COLLECTION_MISSING_AS_ERROR=true``, to keep them as ERROR.

If the filter fails on a span, it exports that span without content
attributes, events or status description; if even that fails, it drops the
span. It never raises into the export path.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping, Optional, Sequence

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

# Attributes in which Cognee 1.6.x exports content (paths in the cognee wheel).
CONTENT_KEYS = frozenset(
    {
        # LLM prompt arguments and response: modules/observability/get_observe.py
        "langfuse.observation.input",
        "langfuse.observation.output",
        # The search question: api/v1/search/search.py, modules/search/methods/search.py
        "memory.query.text",
        "cognee.search.query",
        # Graph query text: databases/graph/ladybug/adapter.py, neo4j_driver/adapter.py
        "cognee.db.query",
    }
)
# With content capture on, where a span's Input and Output come from: fi-collector
# fills them from input.value and output.value only. The first source key that
# is set wins; a span that already has the target key keeps it.
IO_SOURCES = (
    ("input", ("langfuse.observation.input", "memory.query.text", "cognee.search.query")),
    ("output", ("langfuse.observation.output",)),
)
# fi-collector takes the span type from the first of these keys that is set.
SPAN_KIND_KEYS = (
    "fi.span.kind",
    "gen_ai.span.kind",
    "llm.request.type",
    "openinference.span.kind",
)
# Cognee names a span after the decorated method; every
# @observe(as_type="generation") method in Cognee 1.6.5 is acreate_structured_output.
LLM_SPANS = frozenset({"cognee.observe.acreate_structured_output"})
# What replaces error text by default.
DETAIL_REMOVED = "__REDACTED__ (content capture off)"
TYPE_ONLY = "{0} (detail removed: content capture off)"
# A probe of a vector collection that does not exist yet (see the module docstring).
VECTOR_SEARCH_SPAN = "cognee.db.vector.search"
COLLECTION_MISSING = (
    "cognee.infrastructure.databases.vector.exceptions.exceptions.CollectionNotFoundError"
)
COLLECTION_MISSING_KEY = "cognee.vector_search.collection_missing"
# The only event keys whose string values are kept: the exception's class name
# and "True"/"False". Cognee 1.6.5 adds no events other than OpenTelemetry's
# "exception" events; any other event gets the same allowlist.
SAFE_EVENT_KEYS = frozenset({"exception.type", "exception.escaped"})


def span_kind(name: str) -> Optional[str]:
    """The fi.span.kind for a Cognee span name, or None to leave the span as it is."""
    if name == "memory.retrieve" or name.startswith("cognee.search"):
        return "RETRIEVER"
    if name in LLM_SPANS:
        return "LLM"
    # Embedding spans already carry gen_ai.operation.name=embeddings.
    return None


class CogneeExportFilter(SpanExporter):
    """Wrap a span exporter; see the module docstring."""

    def __init__(
        self,
        exporter: SpanExporter,
        capture_content: Optional[bool] = None,
        collection_missing_as_error: Optional[bool] = None,
    ) -> None:
        if capture_content is None:
            capture_content = os.environ.get("COGNEE_FI_CAPTURE_CONTENT", "").lower() == "true"
        if collection_missing_as_error is None:
            collection_missing_as_error = (
                os.environ.get("COGNEE_FI_COLLECTION_MISSING_AS_ERROR", "").lower() == "true"
            )
        self._exporter = exporter
        self._capture_content = capture_content
        self._collection_missing_as_error = collection_missing_as_error

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        out = []
        for span in spans:
            try:
                out.append(self._filtered(span))
            except Exception as error:
                logger.warning(
                    "CogneeExportFilter: %s; exporting the span without content",
                    type(error).__name__,
                )
                try:
                    out.append(self._stripped(span))
                except Exception as error:
                    logger.warning(
                        "CogneeExportFilter: %s; dropping the span", type(error).__name__
                    )
        return self._exporter.export(out)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)

    def _filtered(self, span: ReadableSpan) -> ReadableSpan:
        attributes = dict(span.attributes or {})
        kind = span_kind(span.name)
        if kind and not any(key in attributes for key in SPAN_KIND_KEYS):
            attributes["fi.span.kind"] = kind
        if not self._collection_missing_as_error and _only_collection_missing(span):
            attributes[COLLECTION_MISSING_KEY] = True
            if self._capture_content:
                _add_input_output(attributes)
            else:
                for key in CONTENT_KEYS:
                    attributes.pop(key, None)
            return _copy(span, attributes, (), Status(StatusCode.OK))
        if self._capture_content:
            _add_input_output(attributes)
            events = [
                Event(e.name, _without_stacktrace(e.attributes), e.timestamp) for e in span.events
            ]
            return _copy(span, attributes, events, span.status)
        for key in CONTENT_KEYS:
            attributes.pop(key, None)
        events = [Event(e.name, _without_detail(e.attributes), e.timestamp) for e in span.events]
        status = span.status
        if status.description:
            # Only an ERROR status has a description; name the type of the
            # exception that ended the span, if the span recorded one.
            types = [e.attributes.get("exception.type") for e in events if e.name == "exception"]
            if types and isinstance(types[-1], str):
                status = Status(status.status_code, TYPE_ONLY.format(types[-1]))
            else:
                status = Status(status.status_code, DETAIL_REMOVED)
        return _copy(span, attributes, events, status)

    def _stripped(self, span: ReadableSpan) -> ReadableSpan:
        """The fail-closed copy: no content attributes, no events, no status description."""
        attributes = {k: v for k, v in (span.attributes or {}).items() if k not in CONTENT_KEYS}
        return _copy(span, attributes, (), Status(span.status.status_code))


def _add_input_output(attributes: dict) -> None:
    """Copy captured content to the keys Future AGI shows as Input and Output."""
    for prefix, sources in IO_SOURCES:
        if prefix + ".value" in attributes:
            continue
        value = next((attributes[k] for k in sources if isinstance(attributes.get(k), str)), None)
        if value is None:
            continue
        attributes[prefix + ".value"] = value
        attributes.setdefault(
            prefix + ".mime_type", "application/json" if _is_json(value) else "text/plain"
        )


def _is_json(value: str) -> bool:
    """True for a JSON object or array (Cognee's serialized LLM arguments)."""
    try:
        return isinstance(json.loads(value), (dict, list))
    except ValueError:
        return False


def _only_collection_missing(span: ReadableSpan) -> bool:
    """True for a vector-search span whose only error is a missing collection."""
    if span.name != VECTOR_SEARCH_SPAN or span.status.status_code != StatusCode.ERROR:
        return False
    return bool(span.events) and all(
        event.name == "exception"
        and (event.attributes or {}).get("exception.type") == COLLECTION_MISSING
        for event in span.events
    )


def _without_stacktrace(attributes: Optional[Mapping[str, Any]]) -> dict:
    """An event's attributes as recorded, minus the serialized stack trace (local paths)."""
    return {k: v for k, v in (attributes or {}).items() if k != "exception.stacktrace"}


def _without_detail(attributes: Optional[Mapping[str, Any]]) -> dict:
    """An event's attributes without free text: an allowlist of keys and value types."""
    attributes = attributes or {}
    out = {}
    for key, value in attributes.items():
        if key == "exception.message":
            out[key] = DETAIL_REMOVED
        elif key == "exception.stacktrace":
            # A serialized traceback has no trustworthy boundary between its
            # real frames and traceback-shaped exception-message content.
            continue
        elif key in SAFE_EVENT_KEYS or _not_text(value):
            out[key] = value
    return out


def _not_text(value: Any) -> bool:
    """True for a number or boolean, or a sequence of them."""
    if isinstance(value, (bool, int, float)):
        return True
    return isinstance(value, (list, tuple)) and all(
        isinstance(item, (bool, int, float)) for item in value
    )


def _copy(
    span: ReadableSpan, attributes: Mapping[str, Any], events: Sequence[Event], status: Status
) -> ReadableSpan:
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )
