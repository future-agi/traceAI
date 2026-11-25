"""Committed LiveKit turns for the Future AGI export copy of a call root."""

import json
import sys
from typing import Any, List, Optional

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits
from wrapt import ObjectProxy

try:
    from fi_instrumentation.instrumentation.transcript import allows_conversation_transcript
except ImportError:
    def allows_conversation_transcript(config: TraceConfig) -> bool:
        return not (
            config.hide_inputs or config.hide_input_messages or config.hide_input_text
            or config.hide_outputs or config.hide_output_messages or config.hide_output_text
        )


def _policies() -> List[TraceConfig]:
    """Every policy that applies: the environment's, plus the configuration automatic
    instrumentation resolved (init(config=) or futureagi.toml) when it is active."""
    policies = [TraceConfig()]
    manager = sys.modules.get("fi_instrumentation.auto._manager")
    route = getattr(getattr(manager, "_state", None), "trace_config", None)
    if route is not None:
        policies.append(route)
    return policies


def _transcript(span: ReadableSpan) -> Optional[str]:
    from livekit.agents import get_job_context
    from livekit.agents.telemetry.session_context import session_root_context

    attrs = span.attributes or {}
    # Both native roots carry this key in 1.8.6. Check its presence AFTER LiveKit's
    # filter, never its value: this honors even an explicit per-provider PII opt-out.
    if "lk.pii.room_name" not in iter(attrs):
        return None
    ctx = get_job_context(required=False)
    if ctx is None or not ctx.job.id or attrs.get("lk.job_id") != ctx.job.id:
        return None
    session = ctx.primary_session  # raises if this job never started a session
    if ctx.job.enable_redaction or session.options.recording_options["redaction"]:
        return None
    if span.name == "agent_session":
        # LiveKit clears the primary session's root context just AFTER ending its span.
        root = session_root_context(ctx)
        if root is None or trace.get_current_span(root).get_span_context() != span.context:
            return None

    policies = _policies()
    if not all(allows_conversation_transcript(policy) for policy in policies):
        return None
    turns = []
    for message in session.history.messages():
        role = message.role
        if role not in ("user", "assistant"):
            continue
        text = message.text_content
        if not text or not text.strip():
            continue
        key = SpanAttributes.INPUT_VALUE if role == "user" else SpanAttributes.OUTPUT_VALUE
        masked = text
        for policy in policies:
            masked = policy.mask(key, masked)
            if not isinstance(masked, str):
                return None
        if not masked.strip():
            return None
        turns.append({"role": role, "content": masked})
    return json.dumps(turns, ensure_ascii=False) if turns else None


class _TranscriptRoot(ObjectProxy):
    """Carry masked JSON through batching without adding any attribute to the shared span.

    Ordinary processor behavior (including FI's span status update) still sees the span.
    Only the mapped exporter consumes the payload, when it builds its detached copy.
    """

    def __init__(self, span: ReadableSpan, transcript: str) -> None:
        super().__init__(span)
        self._self_transcript = transcript  # ObjectProxy keeps _self_* on the proxy


class _TranscriptOnEnd:
    def __init__(self, processor: Any, exporter: Any) -> None:
        self.processor = processor
        self.exporter = exporter
        self.delegate = processor.on_end

    def __call__(self, span: ReadableSpan) -> None:
        try:
            if (
                span.parent is None
                and span.name in ("job_entrypoint", "agent_session")
                and span.instrumentation_scope.name == "livekit-agents"
                and _exporter_of(self.processor) is self.exporter
            ):
                value = _transcript(span)
                # Held to the span's own value-length limit. Too long is omitted, never cut:
                # a truncated JSON prefix is not a transcript.
                limit = _value_limit(span)
                if value is not None and (limit is None or len(value) <= limit):
                    span = _TranscriptRoot(span, value)
        except Exception:
            # An unavailable session/API or failed mask omits the transcript, not the span.
            pass
        self.delegate(span)


def _value_limit(span: ReadableSpan) -> Optional[int]:
    """The span's attribute value-length limit; None means unlimited.

    on_end receives a read-only copy without the span's limits, but its attributes keep
    the limit the provider applied, including one set in code rather than the environment.
    """
    attributes = span.attributes
    if hasattr(attributes, "max_value_len"):
        return attributes.max_value_len
    return SpanLimits().max_span_attribute_length


def _exporter_of(processor: Any) -> Any:
    exporter = getattr(processor, "span_exporter", None)
    return exporter if exporter is not None else getattr(processor, "_exporter", None)


def _enable_transcripts(processor: Any, exporter: Any) -> None:
    if isinstance(processor.on_end, _TranscriptOnEnd):
        processor.on_end.exporter = exporter
    else:
        processor.on_end = _TranscriptOnEnd(processor, exporter)
