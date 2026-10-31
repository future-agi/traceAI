"""Wrappers for ``AsyncLive.connect`` and supported ``AsyncSession`` methods.

``Live.send`` and ``Live.start_stream`` are deprecated upstream and are not wrapped.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY

from ._redact import ContentPolicy, _get, as_list, instruction_text, tool_names, url_path
from ._session import SessionTrace

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_STATE_ATTR = "_fi_gemini_live_trace"
_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


class _Instrumentation:
    """Shared state for one ``instrument()`` call."""

    def __init__(self, *, tracer: Any, tracer_provider: Any, policy: ContentPolicy,
                 session_id: Optional[str], user_id: Optional[str],
                 resume_window_seconds: float, flush_timeout_millis: int) -> None:
        self.tracer = tracer
        self.tracer_provider = tracer_provider
        self.policy = policy
        self.session_id = session_id
        self.user_id = user_id
        self.resume_window_seconds = resume_window_seconds
        self.flush_timeout_millis = flush_timeout_millis
        # Resumption handle -> open or detached session. Memory only.
        self._by_handle: Dict[str, SessionTrace] = {}
        self._live: set = set()
        self.flush_count = 0

    # handles -------------------------------------------------------------
    def remember_handle(self, trace: SessionTrace, handle: str) -> None:
        if trace.handle is not None:
            self._by_handle.pop(trace.handle, None)
        trace.handle = handle
        trace.handle_resumable = True
        if not trace.closed:
            self._by_handle[handle] = trace

    def lookup(self, handle: Optional[str]) -> Optional[SessionTrace]:
        if not handle:
            return None
        trace = self._by_handle.get(handle)
        if trace is None or trace.closed:
            return None
        return trace

    def track(self, trace: SessionTrace) -> None:
        self._live.add(trace)

    def forget(self, trace: SessionTrace) -> None:
        self._live.discard(trace)
        if trace.handle is not None:
            self._by_handle.pop(trace.handle, None)
        trace.handle = None

    def close_all(self) -> None:
        """End every session still open or waiting for a resume (process exit, uninstrument)."""
        for trace in list(self._live):
            try:
                if trace.detached_ns is not None:
                    trace.expire()
                else:
                    trace.close(error=None)
            except Exception:
                pass

    def flush(self) -> None:
        """One flush per closed session, off the event loop, never blocking the socket reader."""
        provider = self.tracer_provider
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is None:
            return
        self.flush_count += 1
        timeout = self.flush_timeout_millis

        def run() -> None:
            try:
                force_flush(timeout)
            except Exception:
                pass

        threading.Thread(target=run, name="fi-gemini-live-flush", daemon=False).start()


# ---------------------------------------------------------------------- config parsing
def _modalities_include_audio(config: Any) -> bool:
    modalities = _get(config, "response_modalities")
    if modalities is None:
        modalities = _get(_get(config, "generation_config"), "response_modalities")
    if not modalities:
        # The SDK documents AUDIO as the default when nothing is requested.
        return True
    for m in as_list(modalities):
        name = getattr(m, "value", None) or getattr(m, "name", None) or str(m)
        if str(name).upper().endswith("AUDIO"):
            return True
    return False


def _setup_info(config: Any) -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    modalities = _get(config, "response_modalities") or _get(_get(config, "generation_config"),
                                                              "response_modalities")
    if modalities:
        info["gemini.response_modalities"] = [
            str(getattr(m, "value", None) or getattr(m, "name", None) or m) for m in as_list(modalities)]
    voice = _get(_get(_get(_get(config, "speech_config"), "voice_config"), "prebuilt_voice_config"),
                 "voice_name")
    if voice:
        info["gemini.voice_name"] = str(voice)
    if _get(config, "context_window_compression") is not None:
        info["gemini.context_window_compression"] = True
    if _get(config, "session_resumption") is not None:
        info["gemini.session_resumption"] = True
    if _get(config, "input_audio_transcription") is not None:
        info["gemini.input_audio_transcription"] = True
    if _get(config, "output_audio_transcription") is not None:
        info["gemini.output_audio_transcription"] = True
    names = tool_names(_get(config, "tools"))
    if names:
        info["gemini.tool_names"] = names
    return info


def _system_instruction(config: Any) -> Optional[str]:
    return instruction_text(_get(config, "system_instruction"))


def _resume_handle(config: Any) -> Optional[str]:
    handle = _get(_get(config, "session_resumption"), "handle")
    return str(handle) if handle else None


# ---------------------------------------------------------------------- connect
class _TracedConnect:
    """Async context manager returned in place of the SDK's ``connect`` context manager."""

    def __init__(self, inst: _Instrumentation, wrapped: Any, instance: Any, args: Any,
                 kwargs: Dict[str, Any]) -> None:
        self._inst = inst
        self._cm = wrapped(*args, **kwargs)
        self._instance = instance
        self._kwargs = kwargs
        self._trace: Optional[SessionTrace] = None
        self._session: Any = None

    async def __aenter__(self) -> Any:
        trace = None
        setup_span = None
        reconnect_span = None
        try:
            trace, setup_span, reconnect_span = self._begin()
        except Exception:
            logger.debug("gemini live: session not traced", exc_info=False)
            trace = None
        try:
            session = await self._cm.__aenter__()
        except BaseException as exc:
            if trace is not None:
                try:
                    if reconnect_span is not None:
                        trace.end_reconnect(reconnect_span, ok=False, exc=exc)
                    trace.fail_setup(setup_span, exc, resumed=reconnect_span is not None)
                except Exception:
                    logger.debug("gemini live: setup failure not recorded", exc_info=False)
            raise
        if trace is not None:
            try:
                trace.end_setup(setup_span, session)
                if reconnect_span is not None:
                    trace.end_reconnect(reconnect_span, ok=True)
                setattr(session, _STATE_ATTR, trace)
            except Exception:
                logger.debug("gemini live: setup not recorded", exc_info=False)
        self._trace = trace
        self._session = session
        return session

    def _begin(self):
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return None, None, None
        api_client = getattr(self._instance, "_api_client", None)
        if getattr(api_client, "vertexai", False):
            _warn_once("vertex", "traceai-google-genai-live: Vertex AI Live sessions are not "
                                 "supported; this session is not traced.")
            return None, None, None
        api_key = getattr(api_client, "api_key", None)
        if isinstance(api_key, str) and api_key.startswith("auth_tokens/"):
            _warn_once("ephemeral", "traceai-google-genai-live: ephemeral-token sessions "
                                    "(BidiGenerateContentConstrained) are not supported; "
                                    "this session is not traced.")
            return None, None, None
        model = self._kwargs.get("model")
        config = self._kwargs.get("config")
        handle = _resume_handle(config)
        version = getattr(getattr(api_client, "_http_options", None), "api_version", None) or "v1beta"
        path = url_path(f"/ws/google.ai.generativelanguage.{version}.GenerativeService.BidiGenerateContent")
        inst = self._inst
        trace = inst.lookup(handle)
        reconnect_span = None
        if trace is not None:
            reconnect_span = trace.start_reconnect()
        else:
            from fi_instrumentation.fi_types import SpanAttributes

            session_id = context_api.get_value(SpanAttributes.SESSION_ID) or inst.session_id
            user_id = context_api.get_value(SpanAttributes.USER_ID) or inst.user_id
            info = _setup_info(config)
            if handle:
                info["gemini.resumed_unknown_handle"] = True
            trace = SessionTrace(inst, model=str(model) if model else None,
                                 audio=_modalities_include_audio(config), setup_info=info,
                                 session_id=str(session_id) if session_id else None,
                                 user_id=str(user_id) if user_id else None)
            inst.track(trace)
        setup_span = trace.start_setup(system_instruction=_system_instruction(config), path=path,
                                       resumed=reconnect_span is not None)
        return trace, setup_span, reconnect_span

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            result = await self._cm.__aexit__(exc_type, exc, tb)
        except BaseException as inner:
            self._finish(inner)
            raise
        self._finish(None if result else exc)
        return result

    def _finish(self, exc: Optional[BaseException]) -> None:
        trace = self._trace
        if trace is None:
            return
        self._trace = None
        try:
            if self._session is not None and getattr(self._session, _STATE_ATTR, None) is trace:
                setattr(self._session, _STATE_ATTR, None)
        except Exception:
            pass
        trace.on_connection_exit(exc)


def _trace_of(instance: Any) -> Optional[SessionTrace]:
    trace = getattr(instance, _STATE_ATTR, None)
    if trace is None or trace.closed:
        return None
    return trace


class ConnectWrapper:
    def __init__(self, inst: _Instrumentation) -> None:
        self._inst = inst

    def __call__(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        return _TracedConnect(self._inst, wrapped, instance, args, kwargs)


class ClientContentWrapper:
    async def __call__(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        trace = _trace_of(instance)
        start = time.time_ns()
        result = await wrapped(*args, **kwargs)
        if trace is not None:
            trace.on_client_content(kwargs.get("turns"), kwargs.get("turn_complete", True), start)
        return result


class RealtimeInputWrapper:
    async def __call__(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        trace = _trace_of(instance)
        start = time.time_ns()
        result = await wrapped(*args, **kwargs)
        if trace is not None:
            trace.on_realtime_input(kwargs, start)
        return result


class ToolResponseWrapper:
    async def __call__(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        trace = _trace_of(instance)
        result = await wrapped(*args, **kwargs)
        if trace is not None:
            trace.on_tool_response(kwargs.get("function_responses"))
        return result


class ContextUpdateWrapper:
    async def __call__(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        trace = _trace_of(instance)
        update = None
        if trace is not None:
            try:
                update = trace.start_context_update(
                    system_instruction=kwargs.get("system_instruction"), tools=kwargs.get("tools"))
            except Exception:
                logger.debug("gemini live: context update not traced", exc_info=False)
        try:
            result = await wrapped(*args, **kwargs)
        except BaseException as exc:
            if update is not None:
                trace.end_context_update(update, exc)
            raise
        if update is not None:
            trace.end_context_update(update)
        return result


class ReceiveWrapper:
    def __call__(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        return _traced_receive(wrapped(*args, **kwargs), instance)


async def _traced_receive(agen: Any, instance: Any):
    trace = _trace_of(instance)
    try:
        async for message in agen:
            if trace is not None:
                trace.on_server_message(message)
            yield message
    except Exception as exc:
        if trace is not None:
            trace.on_receive_error(exc)
        raise
    finally:
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
