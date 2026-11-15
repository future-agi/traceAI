"""Bridge call-local FI session context into LiveKit's native SDK spans.

Keep identifiers under LiveKit's PII namespace until our exporter maps a copy.
There is no provider/process-wide session value or trace-id registry: a late child
can inherit only from its actual in-process parent, not from another call.
"""

from typing import Optional

from opentelemetry import context, trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider

from fi_instrumentation.fi_types import SpanAttributes

_SESSION = "lk.pii.traceai.session_id"
_CONVERSATION = "lk.pii.traceai.conversation_id"


class _SessionContextProcessor(SpanProcessor):
    def on_start(self, span: Span, parent_context: Optional[context.Context] = None) -> None:
        scope = span.instrumentation_scope
        if scope is None or scope.name != "livekit-agents":
            return

        attributes = span.attributes or {}
        parent = trace.get_current_span(parent_context)
        inherited = getattr(parent, "attributes", None) or {}
        # An explicit OTel context is authoritative, including an empty context.
        # Read on creation, never on export (which may run in a batch thread).
        session = attributes.get(SpanAttributes.SESSION_ID)
        if session is None:
            session = context.get_value(SpanAttributes.SESSION_ID, parent_context)
        if session is None:
            session = inherited.get(_SESSION, inherited.get(SpanAttributes.SESSION_ID))
        if session is None:
            return

        # Do not copy an explicit unqualified attribute: the application owns it.
        # Its presence also prevents an ambient session from supplying an alias.
        if SpanAttributes.SESSION_ID not in attributes and _SESSION not in attributes:
            span.set_attribute(_SESSION, session)
        if SpanAttributes.GEN_AI_CONVERSATION_ID not in attributes and _CONVERSATION not in attributes:
            span.set_attribute(_CONVERSATION, session)

    def on_end(self, span: ReadableSpan) -> None:
        pass


def _install_session_context(provider: TracerProvider) -> None:
    """Install once per provider without retaining any call's context."""
    active = getattr(provider, "_active_span_processor", None)
    processors = getattr(active, "_span_processors", (active,))
    if not any(isinstance(p, _SessionContextProcessor) for p in processors):
        provider.add_span_processor(_SessionContextProcessor())
