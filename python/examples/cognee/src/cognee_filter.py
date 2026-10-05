"""Export filter for Cognee's spans: content off by default, search and LLM kinds set.

Recipe code, not a package: copy this file next to your app. It wraps the
exporter that sends spans to Future AGI (see app.py)::

    exporter = CogneeExportFilter(HTTPSpanExporter())
    provider.add_span_processor(BatchSpanProcessor(exporter))

and changes only what that exporter sends. It exports copies, so Cognee's
own span buffer and any other processor still see the original spans.

Content. Cognee 1.6.2 writes prompts, document text, answers and search
questions to span attributes and has no setting to stop that. By default the
filter removes those attributes (``CONTENT_KEYS``) and, in the same span's
status description and event attributes (exception messages and stack
traces), replaces their text with ``__REDACTED__``. Pass
``capture_content=True``, or set ``COGNEE_FI_CAPTURE_CONTENT=true``, to export
everything.

Span kinds. Cognee sets no span-kind attribute, so Future AGI would store
search and LLM spans as ``unknown``. The filter sets ``fi.span.kind`` by span
name (``span_kind``), only where none of ``SPAN_KIND_KEYS`` is set.

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
from opentelemetry.trace import Status

logger = logging.getLogger(__name__)

# Attributes in which Cognee 1.6.2 exports content (paths in the cognee wheel).
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
# fi-collector takes the span type from the first of these keys that is set.
SPAN_KIND_KEYS = (
    "fi.span.kind",
    "gen_ai.span.kind",
    "llm.request.type",
    "openinference.span.kind",
)
# Cognee names a span after the decorated method; every
# @observe(as_type="generation") method in Cognee 1.6.2 is acreate_structured_output.
LLM_SPANS = frozenset({"cognee.observe.acreate_structured_output"})
REDACTED = "__REDACTED__"


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

    def __init__(self, exporter: SpanExporter, capture_content: Optional[bool] = None) -> None:
        if capture_content is None:
            capture_content = os.environ.get("COGNEE_FI_CAPTURE_CONTENT", "").lower() == "true"
        self._exporter = exporter
        self._capture_content = capture_content

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
        if self._capture_content:
            return _copy(span, attributes, span.events, span.status)
        texts = _content_texts([attributes.pop(key) for key in CONTENT_KEYS if key in attributes])
        events = [Event(e.name, _redacted(e.attributes, texts), e.timestamp) for e in span.events]
        status = span.status
        if status.description:
            status = Status(status.status_code, _redact(status.description, texts))
        return _copy(span, attributes, events, status)

    def _stripped(self, span: ReadableSpan) -> ReadableSpan:
        """The fail-closed copy: no content attributes, no events, no status description."""
        attributes = {k: v for k, v in (span.attributes or {}).items() if k not in CONTENT_KEYS}
        return _copy(span, attributes, (), Status(span.status.status_code))


def _content_texts(values: Sequence[Any]) -> list[str]:
    """The text to redact: each removed value and, for a JSON object, its string fields."""
    texts = set()
    for value in map(str, values):
        texts.add(value)
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            texts.update(field for field in parsed.values() if isinstance(field, str))
    # Longest first, so a field inside a longer value does not split it.
    return sorted((text for text in texts if text.strip()), key=len, reverse=True)


def _redact(text: str, texts: Sequence[str]) -> str:
    for content in texts:
        text = text.replace(content, REDACTED)
    return text


def _redacted(attributes: Optional[Mapping[str, Any]], texts: Sequence[str]) -> dict:
    return {
        key: _redact(value, texts) if isinstance(value, str) else value
        for key, value in (attributes or {}).items()
        if key not in CONTENT_KEYS
    }


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
