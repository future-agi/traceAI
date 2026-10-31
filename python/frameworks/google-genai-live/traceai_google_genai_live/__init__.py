"""Future AGI tracing for Gemini Live sessions opened with ``client.aio.live.connect``.

Gemini Developer API only. One Live session is one trace, including a reconnect that
passes the resumption handle this process stored. Audio bytes, API keys and resumption
handles never land on a span.
"""

from __future__ import annotations

import atexit
import logging
from typing import Any, Collection, Optional

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.trace import get_tracer, get_tracer_provider
from wrapt import wrap_function_wrapper

from fi_instrumentation import FITracer, TraceConfig
from traceai_google_genai_live._connect import (
    ClientContentWrapper,
    ConnectWrapper,
    ContextUpdateWrapper,
    RealtimeInputWrapper,
    ReceiveWrapper,
    ToolResponseWrapper,
    _Instrumentation,
)
from traceai_google_genai_live._redact import policy_from
from traceai_google_genai_live.package import _instruments
from traceai_google_genai_live.version import __version__

__all__ = ["GoogleGenAILiveInstrumentor", "instrument", "__version__", "PATCHED"]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "google.genai.live"
# Everything this package patches. `Live.send`, `Live.start_stream`, `Models.generate_content`
# and every LiveKit / Pipecat class are deliberately absent.
PATCHED = (
    ("AsyncLive", "connect"),
    ("AsyncSession", "send_client_content"),
    ("AsyncSession", "send_realtime_input"),
    ("AsyncSession", "send_tool_response"),
    ("AsyncSession", "send_context_update"),  # optional on older supported SDKs
    ("AsyncSession", "receive"),
)


class GoogleGenAILiveInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrument ``google.genai.live.AsyncLive.connect`` and the session methods that remain.

    Keyword arguments to ``instrument()``:

    - ``tracer_provider``: the provider from ``fi_instrumentation.register()``.
    - ``config``: a ``TraceConfig``; without one the standard ``FI_HIDE_*`` environment
      variables apply. ``hide_inputs`` / ``hide_input_messages`` / ``hide_input_text``
      drop the input side (system instruction, user text and transcription, tool
      arguments); ``hide_outputs`` / ``hide_output_messages`` / ``hide_output_text`` drop
      the output side (model text and transcription, tool results). Both sides off leaves
      metadata only: model, voice name, timings, tool names, flags and token counts. Raw
      error text and the conversation transcript need both sides on.
    - ``session_id`` / ``user_id``: defaults for every session. ``fi_instrumentation``'s
      ``using_session`` / ``using_user`` context managers override them per connect.
    - ``resume_window_seconds``: how long a session whose connection ended after
      ``goAway`` (or died) waits for a reconnect with its stored handle. Default 30.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider = kwargs.get("tracer_provider") or get_tracer_provider()
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError("config must be a fi_instrumentation.TraceConfig")
        tracer = FITracer(get_tracer(__name__, __version__, tracer_provider), config=config)
        self._inst = _Instrumentation(
            tracer=tracer,
            tracer_provider=tracer_provider,
            policy=policy_from(config),
            session_id=kwargs.get("session_id"),
            user_id=kwargs.get("user_id"),
            resume_window_seconds=float(kwargs.get("resume_window_seconds", 30.0)),
            flush_timeout_millis=int(kwargs.get("flush_timeout_millis", 5000)),
        )

        import google.genai.live as live  # noqa: F401  (fail loudly if the SDK moved)

        self._originals = {}
        wrappers = {
            ("AsyncLive", "connect"): ConnectWrapper(self._inst),
            ("AsyncSession", "send_client_content"): ClientContentWrapper(),
            ("AsyncSession", "send_realtime_input"): RealtimeInputWrapper(),
            ("AsyncSession", "send_tool_response"): ToolResponseWrapper(),
            ("AsyncSession", "send_context_update"): ContextUpdateWrapper(),
            ("AsyncSession", "receive"): ReceiveWrapper(),
        }
        for (cls_name, method), wrapper in wrappers.items():
            cls = getattr(live, cls_name)
            if method == "send_context_update" and not hasattr(cls, method):
                continue  # e.g. google-genai 2.28: keep all existing tracing working
            self._originals[(cls_name, method)] = cls.__dict__[method]
            wrap_function_wrapper(_MODULE, f"{cls_name}.{method}", wrapper)
        # Sessions still waiting for a resume are closed before the provider shuts down
        # (atexit runs in reverse registration order and register() came first).
        self._atexit = self._inst.close_all
        atexit.register(self._atexit)

    def _uninstrument(self, **kwargs: Any) -> None:
        import google.genai.live as live

        inst = getattr(self, "_inst", None)
        if inst is not None:
            inst.close_all()
        for (cls_name, method), original in getattr(self, "_originals", {}).items():
            setattr(getattr(live, cls_name), method, original)
        self._originals = {}
        if getattr(self, "_atexit", None) is not None:
            atexit.unregister(self._atexit)
            self._atexit = None


def instrument(
    tracer_provider: Any = None,
    *,
    config: Optional[TraceConfig] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    resume_window_seconds: float = 30.0,
) -> GoogleGenAILiveInstrumentor:
    """Shorthand for ``GoogleGenAILiveInstrumentor().instrument(...)``. Returns the instrumentor."""
    instrumentor = GoogleGenAILiveInstrumentor()
    instrumentor.instrument(
        tracer_provider=tracer_provider,
        config=config,
        session_id=session_id,
        user_id=user_id,
        resume_window_seconds=resume_window_seconds,
    )
    return instrumentor
