"""Future AGI tracing for OpenAI Realtime sessions opened with the official Python SDK.

    from fi_instrumentation import register
    from traceai_openai_realtime import OpenAIRealtimeInstrumentor

    provider = register(project_name="my-voice-agent")
    OpenAIRealtimeInstrumentor().instrument(tracer_provider=provider)

Wraps ``client.realtime.connect`` (``Realtime.connect`` and
``AsyncRealtime.connect``) and nothing else: not ``OpenAI.request`` (that is
traceAI-openai), not ``client.beta.realtime``, not ``client.live`` (GPT-Live),
not LiveKit or Pipecat.
"""

from __future__ import annotations

import atexit
import logging
import weakref
from importlib import import_module
from typing import Any, Collection, Optional

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from wrapt import wrap_function_wrapper

from ._connect import ConnectWrapper, SessionFactory
from ._session import SessionRecorder, Settings
from .package import _instruments
from .version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

__all__ = ["OpenAIRealtimeInstrumentor", "instrument", "__version__"]

_MODULE = "openai.resources.realtime.realtime"
# The only two symbols this package patches (AC-01).
PATCHED = ("Realtime.connect", "AsyncRealtime.connect")

# Sessions still open, so they can be ended before the tracer provider shuts down.
_LIVE: "weakref.WeakSet[SessionRecorder]" = weakref.WeakSet()
_ATEXIT_REGISTERED = False


def _end_open_sessions() -> None:
    for recorder in list(_LIVE):
        recorder.finish("process_exit")


class OpenAIRealtimeInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrumentor for classic OpenAI Realtime WebSocket sessions.

    ``instrument()`` keyword arguments:

    * ``tracer_provider``: the provider from ``fi_instrumentation.register()``.
    * ``config``: a ``fi_instrumentation.TraceConfig``, the same privacy switch every
      traceAI instrumentor uses. Content is captured by default. ``hide_inputs``
      (``FI_HIDE_INPUTS``), ``hide_input_messages`` (``FI_HIDE_INPUT_MESSAGES``) or
      ``hide_input_text`` (``FI_HIDE_INPUT_TEXT``) drop user transcripts and text,
      instructions and tool arguments; ``hide_outputs``, ``hide_output_messages`` or
      ``hide_output_text`` (``FI_HIDE_OUTPUTS`` / ``FI_HIDE_OUTPUT_MESSAGES`` /
      ``FI_HIDE_OUTPUT_TEXT``) drop assistant transcripts and text, tool results and
      error messages. Either side drops the combined conversation transcript. Names,
      timings, token counts, tool names, error types and codes, model and voice stay.
    * ``session_id`` / ``user_id``: written on every session root. A
      ``using_session`` / ``using_user`` context around ``connect()`` wins over
      these, and these win over the OpenAI session id.
    """

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        global _ATEXIT_REGISTERED
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a fi_instrumentation.TraceConfig")
        settings = Settings(
            capture_input=not _hides(config, "hide_inputs", "hide_input_messages", "hide_input_text"),
            capture_output=not _hides(config, "hide_outputs", "hide_output_messages", "hide_output_text"),
            session_id=_text(kwargs.get("session_id")),
            user_id=_text(kwargs.get("user_id")),
        )
        tracer = FITracer(trace_api.get_tracer(__name__, __version__, tracer_provider), config=config)
        factory = SessionFactory(tracer, settings, tracer_provider, _LIVE)

        module = import_module(_MODULE)
        self._originals = (module.Realtime.connect, module.AsyncRealtime.connect)
        wrap_function_wrapper(_MODULE, "Realtime.connect", ConnectWrapper(factory, is_async=False))
        wrap_function_wrapper(_MODULE, "AsyncRealtime.connect", ConnectWrapper(factory, is_async=True))
        if not _ATEXIT_REGISTERED:
            # Registered after register() set up the provider's own exit hook, so it
            # runs first: open sessions end, then the provider flushes them.
            atexit.register(_end_open_sessions)
            _ATEXIT_REGISTERED = True

    def _uninstrument(self, **kwargs: Any) -> None:
        module = import_module(_MODULE)
        sync_connect, async_connect = self._originals
        module.Realtime.connect = sync_connect
        module.AsyncRealtime.connect = async_connect


def instrument(
    tracer_provider: Any = None,
    *,
    config: Optional[TraceConfig] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> OpenAIRealtimeInstrumentor:
    """Shorthand for ``OpenAIRealtimeInstrumentor().instrument(...)``."""
    instrumentor = OpenAIRealtimeInstrumentor()
    instrumentor.instrument(
        tracer_provider=tracer_provider,
        config=config,
        session_id=session_id,
        user_id=user_id,
    )
    return instrumentor


def _hides(config: TraceConfig, *flags: str) -> bool:
    """True when any of the TraceConfig hide flags (or their FI_HIDE_* env vars) is on.
    In a Realtime session the transcripts are the messages, so hiding the messages or
    their text hides the transcripts too."""
    return any(bool(getattr(config, flag, False)) for flag in flags)


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None
