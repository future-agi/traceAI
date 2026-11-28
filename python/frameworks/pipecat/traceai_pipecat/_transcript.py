"""Pipecat setup file for adding committed turns to the worker's conversation span."""

import json
import os
import sys

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace import SpanLimits

try:
    from fi_instrumentation.instrumentation.transcript import allows_conversation_transcript
except ImportError:  # Older core releases use the same six content switches.
    def allows_conversation_transcript(config):
        return not (
            config.hide_inputs or config.hide_input_messages or config.hide_input_text
            or config.hide_outputs or config.hide_output_messages or config.hide_output_text
        )


# Child processes inherit PIPECAT_SETUP_FILES; only a process that installed the hook
# itself (and so chose its own policy) captures a transcript.
_OWNER = "_TRACEAI_PIPECAT_SETUP_PID"


def _install_setup_file():
    path = os.path.abspath(__file__)
    files = os.environ.get("PIPECAT_SETUP_FILES", "")
    if not any(os.path.abspath(f) == path for f in files.split(os.pathsep) if f):
        os.environ["PIPECAT_SETUP_FILES"] = files + os.pathsep + path if files else path
    os.environ[_OWNER] = str(os.getpid())


def _value_limit(span):
    """The span's attribute value-length limit; None means unlimited."""
    limits = getattr(span, "_limits", None)
    if limits is not None:
        return limits.max_span_attribute_length
    attributes = getattr(span, "attributes", None)
    if hasattr(attributes, "max_value_len"):
        return attributes.max_value_len
    return SpanLimits().max_span_attribute_length


async def setup_worker_runner(runner):
    """Pipecat also loads this file for runners; only workers need an observer."""


async def setup_pipeline_worker(worker):
    try:
        from pipecat.evals.conversation_observer import EvalConversationObserver
    except ImportError:  # Pipecat before 1.13 has no committed-turn observer.
        return
    tracing = worker.turn_trace_observer
    if tracing is None or os.environ.get(_OWNER) != str(os.getpid()):
        return

    # Manual mapping uses environment defaults; auto-instrumentation has already
    # resolved init(config=) and file settings. Do not replace those with defaults.
    config = None
    if "fi_instrumentation.auto._manager" in sys.modules:
        from fi_instrumentation.auto._manager import _state

        config = _state.trace_config
    if config is None:
        config = TraceConfig()
    if not allows_conversation_transcript(config):
        return
    observer = EvalConversationObserver()

    # The conversation roots that were live while turns were observed. If the application
    # starts a new root mid-run, the turns' owner is uncertain and nothing is written.
    roots = set()
    observe = getattr(observer, "on_push_frame", None)
    if observe is not None:

        async def on_push_frame(data):
            live = getattr(tracing, "_conversation_span", None)
            if live is not None:
                roots.add(live.get_span_context().span_id)
            await observe(data)

        observer.on_push_frame = on_push_frame

    @observer.event_handler("on_conversation_ended")
    async def write_transcript(_, message):
        span = getattr(tracing, "_conversation_span", None)
        if span is None or not span.is_recording() or getattr(span, "parent", True) is not None:
            return
        if roots and roots != {span.get_span_context().span_id}:
            return
        rows = []
        for turn in message.conversation:
            role = turn.get("role")
            if role not in ("user", "assistant"):
                continue
            content = turn.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            key = SpanAttributes.INPUT_VALUE if role == "user" else SpanAttributes.OUTPUT_VALUE
            try:
                masked = config.mask(key, content)
            except Exception:  # A failed mask must never leave a partial transcript.
                return
            if not isinstance(masked, str):
                return
            if masked.strip():
                rows.append({"role": role, "content": masked})
        if not rows:
            return
        value = json.dumps(rows, ensure_ascii=False)
        limit = _value_limit(span)
        # Too long is omitted, never cut: a truncated JSON prefix is not a transcript.
        if limit is None or len(value) <= limit:
            span.set_attribute("fi.conversation.transcript", value)

    worker.add_observer(observer)


setup_pipeline_task = setup_pipeline_worker  # Older Pipecat looks up only the task hook.
