"""Pipecat setup file for adding committed turns to the worker's conversation span."""

import json
import os
import sys

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes

try:
    from fi_instrumentation.instrumentation.transcript import allows_conversation_transcript
except ImportError:  # Older core releases use the same six content switches.
    def allows_conversation_transcript(config):
        return not (
            config.hide_inputs or config.hide_input_messages or config.hide_input_text
            or config.hide_outputs or config.hide_output_messages or config.hide_output_text
        )


def _install_setup_file():
    path = os.path.abspath(__file__)
    files = os.environ.get("PIPECAT_SETUP_FILES", "")
    if not any(os.path.abspath(f) == path for f in files.split(os.pathsep) if f):
        os.environ["PIPECAT_SETUP_FILES"] = files + os.pathsep + path if files else path


async def setup_worker_runner(runner):
    """Pipecat also loads this file for runners; only workers need an observer."""


async def setup_pipeline_worker(worker):
    try:
        from pipecat.evals.conversation_observer import EvalConversationObserver
    except ImportError:  # Pipecat before 1.13 has no committed-turn observer.
        return
    tracing = worker.turn_trace_observer
    if tracing is None:
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

    @observer.event_handler("on_conversation_ended")
    async def write_transcript(_, message):
        span = getattr(tracing, "_conversation_span", None)
        if span is None or not span.is_recording() or getattr(span, "parent", True) is not None:
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
        if rows:
            span.set_attribute("fi.conversation.transcript", json.dumps(rows, ensure_ascii=False))

    worker.add_observer(observer)


setup_pipeline_task = setup_pipeline_worker  # Older Pipecat looks up only the task hook.
