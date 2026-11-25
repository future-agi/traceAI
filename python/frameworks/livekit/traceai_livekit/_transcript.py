"""Committed LiveKit turns for the Future AGI export copy of a call root."""

import json
from typing import Any, Optional

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from wrapt import ObjectProxy

try:
    from fi_instrumentation.instrumentation.transcript import allows_conversation_transcript
except ImportError:
    def allows_conversation_transcript(config: TraceConfig) -> bool:
        return not (
            config.hide_inputs or config.hide_input_messages or config.hide_input_text
            or config.hide_outputs or config.hide_output_messages or config.hide_output_text
        )


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

    config = TraceConfig()  # register()/enable_* use the environment's TraceConfig
    if not allows_conversation_transcript(config):
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
        masked = config.mask(key, text)
        if not isinstance(masked, str) or not masked.strip():
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
                and self.processor.span_exporter is self.exporter
            ):
                value = _transcript(span)
                if value is not None:
                    span = _TranscriptRoot(span, value)
        except Exception:
            # An unavailable session/API or failed mask omits the transcript, not the span.
            pass
        self.delegate(span)


def _enable_transcripts(processor: Any, exporter: Any) -> None:
    if isinstance(processor.on_end, _TranscriptOnEnd):
        processor.on_end.exporter = exporter
    else:
        processor.on_end = _TranscriptOnEnd(processor, exporter)
