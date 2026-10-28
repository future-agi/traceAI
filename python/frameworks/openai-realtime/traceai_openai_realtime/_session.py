"""Turn the events of one Realtime connection into spans.

``SessionRecorder`` sees every server event the connection receives and every
client event it sends, as plain dicts, and knows nothing about sockets. One
recorder is one connection object and one trace:

    openai.realtime.session          root, no parent. CONVERSATION (audio) or CHAIN (text)
      openai.realtime.user_turn      one per user utterance
      openai.realtime.response       one per model response (LLM)
        openai.realtime.tool         one per function call the response announced
      realtime.error                 one per server ``error`` event

Event names and fields follow the GA Realtime reference
(https://developers.openai.com/api/reference/resources/realtime/server-events and
.../client-events, read 2026-10-15) and the openai Python SDK 3.31.0 types. A
cancelled response is not a separate event: the server sends ``response.done``
with ``response.status == "cancelled"``.

Audio bytes are never stored. Audio deltas and appends are counted and their
decoded length is used for durations; the base64 text is dropped on the floor.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

from opentelemetry import trace as trace_api
from opentelemetry.context import Context
from opentelemetry.trace import Link, Status, StatusCode

from ._redact import redact

logger = logging.getLogger(__name__)

# Span names (architecture section 3).
ROOT_NAME = "openai.realtime.session"
TURN_NAME = "openai.realtime.user_turn"
RESPONSE_NAME = "openai.realtime.response"
TOOL_NAME = "openai.realtime.tool"
ERROR_NAME = "realtime.error"

# The collector reads fi.span.kind first when it derives observation_type, and the
# Voice tab lists a parentless root whose observation_type is "conversation".
FI_SPAN_KIND = "fi.span.kind"

PROVIDER = "openai"

# Close reasons that mean the session did not end the way the caller asked it to.
ERROR_REASONS = {"socket_closed", "task_cancelled", "exception", "connect_failed"}
# Close reasons after which the exporter is flushed once.
FLUSH_REASONS = ERROR_REASONS

_DEDUPE_WINDOW = 4096

# Decoded bytes per millisecond of audio for each Realtime audio format.
# GA names (audio.*.format.type) and the beta names (input/output_audio_format).
_BYTES_PER_MS = {
    "audio/pcm": 48.0,  # 24 kHz, 16-bit, mono
    "pcm16": 48.0,
    "audio/pcmu": 8.0,  # G.711, 8 kHz, 8-bit
    "audio/pcma": 8.0,
    "g711_ulaw": 8.0,
    "g711_alaw": 8.0,
}
_DEFAULT_FORMAT = "audio/pcm"

# Beta-era spellings that some servers still send next to the GA names.
_AUDIO_DELTA = {"response.output_audio.delta", "response.audio.delta"}
_AUDIO_TRANSCRIPT_DONE = {"response.output_audio_transcript.done", "response.audio_transcript.done"}
_TEXT_DONE = {"response.output_text.done", "response.text.done"}


@dataclass(frozen=True)
class Settings:
    """What the instrumentor was told at ``instrument()``."""

    capture_input: bool = True
    capture_output: bool = True
    session_id: Optional[str] = None
    user_id: Optional[str] = None

    @property
    def capture_conversation(self) -> bool:
        return self.capture_input and self.capture_output


def _b64_len(data: object) -> int:
    """Decoded length of a base64 string, without decoding it."""
    if not isinstance(data, str) or not data:
        return 0
    pad = 2 if data.endswith("==") else 1 if data.endswith("=") else 0
    return max(0, len(data) * 3 // 4 - pad)


def _fmt(value: object) -> Optional[str]:
    """The format name out of a GA format object or a beta format string."""
    if isinstance(value, dict):
        value = value.get("type")
    return value if isinstance(value, str) and value else None


def _now() -> int:
    return time.time_ns()


@dataclass(eq=False)
class _Turn:
    span: Any
    item_id: Optional[str]
    seq: int
    start_ns: int
    manual: bool = False
    audio_start_ms: Optional[int] = None
    audio_end_ms: Optional[int] = None
    appended_bytes: int = 0
    committed_ns: Optional[int] = None
    transcript: Optional[str] = None


@dataclass(eq=False)
class _Response:
    span: Any
    response_id: Optional[str]
    seq: int
    start_ns: int
    inputs: List[Tuple[str, Callable[[], Optional[str]]]] = field(default_factory=list)
    item_ids: Set[str] = field(default_factory=set)
    audio_delta_count: int = 0
    audio_bytes: int = 0
    transcript: Optional[str] = None
    text: Optional[str] = None
    tool_calls: List[Dict[str, str]] = field(default_factory=list)
    done_ns: Optional[int] = None
    status: Optional[str] = None
    audio_end_ms: Optional[int] = None
    truncation: Optional[str] = None
    output_format: Optional[str] = None


@dataclass(eq=False)
class _Tool:
    span: Any
    call_id: str
    name: Optional[str]


class _Seen:
    """Bounded memory of event ids, so a replayed event does not open a second span."""

    def __init__(self, size: int = _DEDUPE_WINDOW) -> None:
        self._order: Deque[str] = deque()
        self._ids: Set[str] = set()
        self._size = size

    def add(self, event_id: object) -> bool:
        """True the first time an id is seen. Events without an id are always new."""
        if not isinstance(event_id, str) or not event_id:
            return True
        if event_id in self._ids:
            return False
        self._ids.add(event_id)
        self._order.append(event_id)
        if len(self._order) > self._size:
            self._ids.discard(self._order.popleft())
        return True


class SessionRecorder:
    """Spans for one Realtime connection. Every public method is safe to call from
    the socket reader: it never raises and never waits on export."""

    def __init__(
        self,
        tracer: Any,
        settings: Settings,
        *,
        model: Optional[str] = None,
        secrets: Iterable[Optional[str]] = (),
        context_attributes: Optional[Dict[str, Any]] = None,
        flush: Optional[Callable[[], None]] = None,
        on_finish: Optional[Callable[["SessionRecorder"], None]] = None,
    ) -> None:
        self._tracer = tracer
        self._settings = settings
        self._secrets = tuple(s for s in secrets if s)
        self._ctx_attrs = dict(context_attributes or {})
        self._flush = flush
        self._on_finish = on_finish
        self._lock = threading.RLock()
        self._seen_server = _Seen()
        self._seen_client = _Seen()

        self.root: Any = None
        self.finished = False
        self.end_reason: Optional[str] = None
        self._start_ns = 0
        self._seq = 0
        self._model = model if isinstance(model, str) and model else None
        self._openai_session_id: Optional[str] = None
        self._session_id_written = False
        self._voice: Optional[str] = None
        self._output_modalities: Optional[List[str]] = None
        self._output_format: Optional[str] = None
        self._input_format: Optional[str] = None
        self._transcription_on = False
        self._client_instructions: Optional[str] = None
        self._server_instructions: Optional[str] = None
        self._client_closing = False
        self._drops = 0

        self._turns: Dict[str, _Turn] = {}
        self._append_bytes = 0
        self._append_first_ns: Optional[int] = None
        self._responses: Dict[str, _Response] = {}
        self._held: Dict[str, _Response] = {}
        self._anon_response: Optional[_Response] = None
        self._tools: Dict[str, _Tool] = {}
        self._pending_inputs: List[Tuple[str, Callable[[], Optional[str]]]] = []
        # (seq, role, text getter) in conversation order.
        self._transcript: List[Tuple[int, str, Callable[[], Optional[str]]]] = []

        self._audio_seen = False
        self._closed_turns = 0
        self._user_audio_ms = 0.0
        self._assistant_audio_ms = 0.0
        self._assistant_audio_known = True

    # ------------------------------------------------------------------ lifecycle

    def start(self, start_ns: Optional[int] = None) -> None:
        """Open the root span. It never has a parent: the Voice tab lists only a
        parentless conversation root. The caller's active span, if any, is linked."""
        with self._lock:
            if self.root is not None:
                return
            self._start_ns = start_ns or _now()
            links = []
            current = trace_api.get_current_span().get_span_context()
            if current is not None and current.is_valid:
                links.append(Link(current))
            self.root = self._tracer.start_span(
                ROOT_NAME,
                context=Context(),
                links=links,
                start_time=self._start_ns,
            )
            self._set(self.root, "gen_ai.system", PROVIDER)
            self._set(self.root, "gen_ai.provider.name", PROVIDER)
            if self._model:
                self._set(self.root, "gen_ai.request.model", self._model)
            if "session.id" not in self._ctx_attrs and self._settings.session_id:
                self._set(self.root, "session.id", self._settings.session_id)
                self._session_id_written = True
            elif "session.id" in self._ctx_attrs:
                self._session_id_written = True
            if "user.id" not in self._ctx_attrs and self._settings.user_id:
                self._set(self.root, "user.id", self._settings.user_id)

    def connect_failed(self, exc: BaseException, start_ns: Optional[int] = None) -> None:
        """The socket never opened. One root span with status ERROR, then flush."""
        try:
            self.start(start_ns)
            self.finish("connect_failed", exc=exc)
        except Exception:  # pragma: no cover - defensive
            logger.debug("traceai-openai-realtime: connect failure not recorded", exc_info=True)

    def mark_client_close(self) -> None:
        self._client_closing = True

    def on_socket_error(self, exc: BaseException, *, reconnect_possible: bool) -> None:
        """``recv`` raised. A clean close ends the session; an abnormal close ends it
        too unless the SDK was given ``on_reconnecting`` and may open a new socket
        on the same connection object."""
        try:
            name = type(exc).__name__
            clean = name == "ConnectionClosedOK"
            code = _close_code(exc)
            with self._lock:
                if self.finished:
                    return
                if clean:
                    reason = "client_closed" if self._client_closing else "server_closed"
                    self._finish_locked(reason, close_code=code, exc=None)
                    return
                if name != "ConnectionClosedError" and not name.endswith("ConnectionClosedError"):
                    # Not a socket close (for example a cancelled recv). The session goes on.
                    return
                if reconnect_possible and not self._client_closing:
                    self._drops += 1
                    self._close_in_flight("socket_closed", error=True, now=_now())
                    if self.root is not None:
                        self.root.add_event("realtime.socket_dropped", {"realtime.close_code": code} if code else {})
                    return
                self._finish_locked("socket_closed", close_code=code, exc=exc)
        except Exception:
            logger.debug("traceai-openai-realtime: socket error not recorded", exc_info=True)

    def finish(self, reason: str, exc: Optional[BaseException] = None) -> None:
        try:
            with self._lock:
                self._finish_locked(reason, exc=exc)
        except Exception:
            logger.debug("traceai-openai-realtime: session end not recorded", exc_info=True)

    # ------------------------------------------------------------------ inbound

    def on_server_frame(self, data: object) -> None:
        try:
            event = json.loads(data) if isinstance(data, (bytes, bytearray, str)) else data
        except (ValueError, UnicodeDecodeError, RecursionError):
            return
        if isinstance(event, dict):
            self.on_server_event(event)

    def on_server_event(self, event: Dict[str, Any], now: Optional[int] = None) -> None:
        try:
            with self._lock:
                if self.finished or self.root is None or not self._seen_server.add(event.get("event_id")):
                    return
                self._server(event, now or _now())
        except Exception:
            logger.debug("traceai-openai-realtime: server event not recorded", exc_info=True)

    def _server(self, event: Dict[str, Any], now: int) -> None:
        etype = event.get("type")
        if etype in ("session.created", "session.updated"):
            self._apply_session(event.get("session"), created=etype == "session.created")
        elif etype == "input_audio_buffer.speech_started":
            self._speech_started(event, now)
        elif etype == "input_audio_buffer.speech_stopped":
            turn = self._turns.get(event.get("item_id") or "")
            if turn is not None and isinstance(event.get("audio_end_ms"), int):
                turn.audio_end_ms = event["audio_end_ms"]
        elif etype == "input_audio_buffer.committed":
            self._committed(event, now)
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._transcription(event, now, failed=False)
        elif etype == "conversation.item.input_audio_transcription.failed":
            self._transcription(event, now, failed=True)
        elif etype == "response.created":
            self._response_created(event, now)
        elif etype in _AUDIO_DELTA:
            resp = self._response_for(event.get("response_id"))
            if resp is not None:
                resp.audio_delta_count += 1
                resp.audio_bytes += _b64_len(event.get("delta"))
                self._audio_seen = True
                self._track_item(resp, event.get("item_id"))
        elif etype in _AUDIO_TRANSCRIPT_DONE:
            resp = self._response_for(event.get("response_id"))
            if resp is not None and isinstance(event.get("transcript"), str):
                resp.transcript = (resp.transcript + " " if resp.transcript else "") + event["transcript"]
                self._track_item(resp, event.get("item_id"))
        elif etype in _TEXT_DONE:
            resp = self._response_for(event.get("response_id"))
            if resp is not None and isinstance(event.get("text"), str):
                resp.text = (resp.text + " " if resp.text else "") + event["text"]
                self._track_item(resp, event.get("item_id"))
        elif etype in ("response.output_item.added", "response.output_item.done"):
            resp = self._response_for(event.get("response_id"))
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if resp is not None:
                self._track_item(resp, item.get("id"))
        elif etype == "response.function_call_arguments.done":
            self._tool_announced(
                event.get("call_id"), event.get("name"), event.get("arguments"), event.get("response_id"), now
            )
        elif etype == "response.done":
            self._response_done(event, now)
        elif etype == "conversation.item.truncated":
            self._truncated(event.get("item_id"), event.get("audio_end_ms"), confirmed=True)
        elif etype == "error":
            self._error_event(event, now)
        # Everything else (deltas, rate_limits.updated, conversation.item.* echoes) is not a span.

    # ------------------------------------------------------------------ outbound

    def on_client_event(self, event: Any, now: Optional[int] = None) -> None:
        try:
            data = _as_dict(event)
            if data is None:
                return
            with self._lock:
                if self.finished or self.root is None or not self._seen_client.add(data.get("event_id")):
                    return
                self._client(data, now or _now())
        except Exception:
            logger.debug("traceai-openai-realtime: client event not recorded", exc_info=True)

    def _client(self, event: Dict[str, Any], now: int) -> None:
        etype = event.get("type")
        if etype == "session.update":
            session = event.get("session") if isinstance(event.get("session"), dict) else {}
            if isinstance(session.get("instructions"), str):
                self._client_instructions = session["instructions"]
            self._apply_session(session, created=False, from_client=True)
        elif etype == "input_audio_buffer.append":
            size = _b64_len(event.get("audio"))
            if size:
                self._audio_seen = True
                self._append_bytes += size
                if self._append_first_ns is None:
                    self._append_first_ns = now
        elif etype == "input_audio_buffer.clear":
            self._append_bytes = 0
            self._append_first_ns = None
        elif etype == "conversation.item.create":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            self._client_item(item, now)
        elif etype == "conversation.item.truncate":
            self._truncated(event.get("item_id"), event.get("audio_end_ms"), confirmed=False)

    # ------------------------------------------------------------------ session

    def _apply_session(self, session: Any, *, created: bool, from_client: bool = False) -> None:
        if not isinstance(session, dict):
            return
        if not from_client:
            sid = session.get("id")
            if isinstance(sid, str) and sid and (created or self._openai_session_id is None):
                self._openai_session_id = sid
                self._set(self.root, "realtime.openai_session_id", sid)
                if not self._session_id_written:
                    self._set(self.root, "session.id", sid)
                    self._session_id_written = True
            if isinstance(session.get("instructions"), str):
                self._server_instructions = session["instructions"]
        model = session.get("model")
        if isinstance(model, str) and model and not self._model:
            self._model = model
            self._set(self.root, "gen_ai.request.model", model)
        modalities = session.get("output_modalities", session.get("modalities"))
        if isinstance(modalities, list):
            self._output_modalities = [m for m in modalities if isinstance(m, str)]
            self._set(self.root, "realtime.output_modalities", self._output_modalities)
        audio = session.get("audio") if isinstance(session.get("audio"), dict) else {}
        out = audio.get("output") if isinstance(audio.get("output"), dict) else {}
        inp = audio.get("input") if isinstance(audio.get("input"), dict) else {}
        voice = out.get("voice", session.get("voice"))
        if isinstance(voice, str) and voice:
            self._voice = voice
            self._set(self.root, "realtime.voice", voice)
        out_fmt = _fmt(out.get("format")) or _fmt(session.get("output_audio_format"))
        if out_fmt:
            self._output_format = out_fmt
        in_fmt = _fmt(inp.get("format")) or _fmt(session.get("input_audio_format"))
        if in_fmt:
            self._input_format = in_fmt
        if "transcription" in inp or "input_audio_transcription" in session:
            self._transcription_on = bool(inp.get("transcription") or session.get("input_audio_transcription"))

    # ------------------------------------------------------------------ user turns

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _open_turn(self, item_id: Optional[str], start_ns: int, manual: bool) -> _Turn:
        span = self._child(TURN_NAME, start_ns)
        self._set(span, FI_SPAN_KIND, "CHAIN")
        self._set(span, "realtime.turn_detection", "manual" if manual else "server_vad")
        if item_id:
            self._set(span, "realtime.item_id", item_id)
        turn = _Turn(span=span, item_id=item_id, seq=self._next_seq(), start_ns=start_ns, manual=manual)
        self._transcript.append((turn.seq, "user", lambda t=turn: t.transcript))
        if item_id:
            self._turns[item_id] = turn
        return turn

    def _speech_started(self, event: Dict[str, Any], now: int) -> None:
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or item_id in self._turns:
            return
        turn = self._open_turn(item_id, now, manual=False)
        self._audio_seen = True
        if isinstance(event.get("audio_start_ms"), int):
            turn.audio_start_ms = event["audio_start_ms"]

    def _committed(self, event: Dict[str, Any], now: int) -> None:
        item_id = event.get("item_id")
        turn = self._turns.get(item_id) if isinstance(item_id, str) else None
        if turn is None:
            # Push-to-talk: no VAD event opened a turn, the client committed the buffer.
            turn = self._open_turn(item_id if isinstance(item_id, str) else None, self._append_first_ns or now, True)
            turn.appended_bytes = self._append_bytes
        self._append_bytes = 0
        self._append_first_ns = None
        self._audio_seen = True
        turn.committed_ns = now
        self._pending_inputs.append(("user", lambda t=turn: t.transcript))
        # The barge-in turn has been committed; truncates for earlier responses are in by now.
        self._release_held()
        if not self._transcription_on:
            self._set(turn.span, "realtime.transcription", "not_enabled")
            self._close_turn(turn, end_ns=now)

    def _transcription(self, event: Dict[str, Any], now: int, *, failed: bool) -> None:
        turn = self._turns.get(event.get("item_id") or "")
        if turn is None or turn.span is None:
            return
        if failed:
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            self._set(turn.span, "realtime.transcription", "failed")
            self._set_error_fields(turn.span, err)
            turn.span.set_status(Status(StatusCode.ERROR, self._error_description(err)))
        else:
            text = event.get("transcript")
            if isinstance(text, str):
                turn.transcript = text
            self._set(turn.span, "realtime.transcription", "completed")
        self._close_turn(turn, end_ns=turn.committed_ns or now)

    def _close_turn(self, turn: _Turn, *, end_ns: int, counted: bool = True, reason: Optional[str] = None) -> None:
        span = turn.span
        if span is None:
            return
        turn.span = None
        if self._settings.capture_input and turn.transcript:
            self._set(span, "output.value", turn.transcript)
            self._set(span, "output.mime_type", "text/plain")
        duration = self._turn_audio_ms(turn)
        if duration is not None:
            self._set(span, "realtime.audio_duration_ms", round(duration, 1))
            if counted:
                self._user_audio_ms += duration
        if turn.audio_start_ms is not None:
            self._set(span, "realtime.audio_start_ms", turn.audio_start_ms)
        if turn.audio_end_ms is not None:
            self._set(span, "realtime.audio_end_ms", turn.audio_end_ms)
        if reason:
            self._set(span, "realtime.end_reason", reason)
            if reason in ERROR_REASONS:
                span.set_status(Status(StatusCode.ERROR, reason))
        elif counted:
            # A failed transcription already set ERROR; set_status ignores OK after ERROR.
            span.set_status(Status(StatusCode.OK))
        if counted:
            self._closed_turns += 1
        span.end(end_time=end_ns)

    def _turn_audio_ms(self, turn: _Turn) -> Optional[float]:
        if turn.audio_start_ms is not None and turn.audio_end_ms is not None and turn.audio_end_ms >= turn.audio_start_ms:
            return float(turn.audio_end_ms - turn.audio_start_ms)
        if turn.manual and turn.appended_bytes:
            per_ms = _BYTES_PER_MS.get(self._input_format or _DEFAULT_FORMAT)
            if per_ms:
                return turn.appended_bytes / per_ms
        return None

    # ------------------------------------------------------------------ responses

    def _response_created(self, event: Dict[str, Any], now: int) -> None:
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        rid = response.get("id") if isinstance(response.get("id"), str) else None
        if rid and (rid in self._responses or rid in self._held):
            return
        self._release_held()
        span = self._child(RESPONSE_NAME, now)
        self._set(span, FI_SPAN_KIND, "LLM")
        self._set(span, "gen_ai.system", PROVIDER)
        self._set(span, "gen_ai.provider.name", PROVIDER)
        if self._model:
            self._set(span, "gen_ai.request.model", self._model)
        if rid:
            self._set(span, "gen_ai.response.id", rid)
        resp = _Response(
            span=span,
            response_id=rid,
            seq=self._next_seq(),
            start_ns=now,
            inputs=self._pending_inputs,
            output_format=self._output_format,
        )
        self._pending_inputs = []
        self._transcript.append((resp.seq, "assistant", lambda r=resp: r.transcript or r.text))
        if rid:
            self._responses[rid] = resp
        else:
            self._anon_response = resp

    def _response_for(self, rid: object) -> Optional[_Response]:
        if isinstance(rid, str) and rid:
            return self._responses.get(rid) or self._held.get(rid)
        return self._anon_response

    @staticmethod
    def _track_item(resp: _Response, item_id: object) -> None:
        if isinstance(item_id, str) and item_id:
            resp.item_ids.add(item_id)

    def _response_done(self, event: Dict[str, Any], now: int) -> None:
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        rid = response.get("id") if isinstance(response.get("id"), str) else None
        resp = self._responses.pop(rid, None) if rid else None
        if resp is None and self._anon_response is not None and (not rid or self._anon_response.response_id in (None, rid)):
            resp, self._anon_response = self._anon_response, None
        if resp is None:
            return
        resp.done_ns = now
        span = resp.span
        status = response.get("status") if isinstance(response.get("status"), str) else None
        resp.status = status
        details = response.get("status_details") if isinstance(response.get("status_details"), dict) else {}
        reason = details.get("reason") if isinstance(details.get("reason"), str) else None
        if status:
            self._set(span, "realtime.response.status", status)
        if reason:
            self._set(span, "realtime.response.status_reason", reason)
        modalities = response.get("output_modalities", response.get("modalities"))
        if isinstance(modalities, list):
            self._set(span, "realtime.output_modalities", [m for m in modalities if isinstance(m, str)])
        self._usage(span, response.get("usage"))
        self._output_items(resp, response.get("output"), now)
        if status == "cancelled":
            self._set(span, "realtime.cancelled", True)
            span.set_status(Status(StatusCode.OK))
        elif status == "failed":
            err = details.get("error") if isinstance(details.get("error"), dict) else {}
            self._set_error_fields(span, err)
            span.set_status(Status(StatusCode.ERROR, self._error_description(err) or "failed"))
        else:
            span.set_status(Status(StatusCode.OK))
        if resp.audio_delta_count:
            # Hold it: the client may still truncate the audio the user did not hear.
            self._held[rid or f"_anon{resp.seq}"] = resp
        else:
            self._end_response(resp)

    def _output_items(self, resp: _Response, output: Any, now: int) -> None:
        if not isinstance(output, list):
            return
        for item in output:
            if not isinstance(item, dict):
                continue
            self._track_item(resp, item.get("id"))
            if item.get("type") == "function_call":
                self._tool_announced(item.get("call_id"), item.get("name"), item.get("arguments"), resp.response_id, now)
            elif item.get("type") == "message" and not (resp.transcript or resp.text):
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if isinstance(part.get("transcript"), str):
                        resp.transcript = part["transcript"]
                    elif isinstance(part.get("text"), str):
                        resp.text = part["text"]

    def _usage(self, span: Any, usage: Any) -> None:
        """Copied from response.done only. Missing numbers are omitted, never zeroed."""
        if not isinstance(usage, dict):
            return
        for key, attr in (
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
            ("total_tokens", "gen_ai.usage.total_tokens"),
        ):
            if isinstance(usage.get(key), int):
                self._set(span, attr, usage[key])
        inp = usage.get("input_token_details") if isinstance(usage.get("input_token_details"), dict) else {}
        out = usage.get("output_token_details") if isinstance(usage.get("output_token_details"), dict) else {}
        for src, key, attr in (
            (inp, "audio_tokens", "gen_ai.usage.input_tokens.audio"),
            (inp, "text_tokens", "gen_ai.usage.input_tokens.text"),
            (inp, "cached_tokens", "gen_ai.usage.input_tokens.cache_read"),
            (out, "audio_tokens", "gen_ai.usage.output_tokens.audio"),
            (out, "text_tokens", "gen_ai.usage.output_tokens.text"),
        ):
            if isinstance(src.get(key), int):
                self._set(span, attr, src[key])

    def _truncated(self, item_id: object, audio_end_ms: object, *, confirmed: bool) -> None:
        if not isinstance(item_id, str):
            return
        candidates = list(self._held.values()) + list(self._responses.values())
        if self._anon_response is not None:
            candidates.append(self._anon_response)
        resp = next((r for r in candidates if item_id in r.item_ids), None)
        if resp is None:
            return
        if isinstance(audio_end_ms, int):
            resp.audio_end_ms = audio_end_ms
        if confirmed:
            resp.truncation = "server_confirmed"
            if any(held is resp for held in self._held.values()):
                self._end_response(resp)
        elif resp.truncation is None:
            resp.truncation = "client_sent_unconfirmed"

    def _release_held(self) -> None:
        for resp in list(self._held.values()):
            self._end_response(resp)

    def _end_response(self, resp: _Response, *, reason: Optional[str] = None) -> None:
        for key, held in list(self._held.items()):
            if held is resp:
                del self._held[key]
        span = resp.span
        if span is None:
            return
        resp.span = None
        if resp.audio_delta_count:
            self._set(span, "realtime.audio_delta_count", resp.audio_delta_count)
            per_ms = _BYTES_PER_MS.get(resp.output_format or self._output_format or _DEFAULT_FORMAT)
            if per_ms:
                generated = resp.audio_bytes / per_ms
                heard = min(generated, float(resp.audio_end_ms)) if resp.audio_end_ms is not None else generated
                self._set(span, "realtime.audio_duration_ms", round(heard, 1))
                if reason is None:
                    self._assistant_audio_ms += heard
            else:
                self._assistant_audio_known = False
        if resp.audio_end_ms is not None:
            self._set(span, "realtime.audio_end_ms", resp.audio_end_ms)
        if resp.truncation:
            self._set(span, "realtime.truncation", resp.truncation)
        elif resp.status == "cancelled":
            self._set(span, "realtime.truncation", "not_sent")
        self._response_io(span, resp)
        if reason:
            self._set(span, "realtime.end_reason", reason)
            if reason in ERROR_REASONS:
                span.set_status(Status(StatusCode.ERROR, reason))
        else:
            self._closed_turns += 1
        span.end(end_time=resp.done_ns or _now())

    def _response_io(self, span: Any, resp: _Response) -> None:
        """Input: the conversation items added since the previous response (user
        speech or text, tool results). Output: the transcript or text, and the
        function calls the model made. Tool arguments count as input and tool
        results as output, the same as on the tool span."""
        s = self._settings
        if s.capture_input:
            messages = []
            for role, getter in resp.inputs:
                if role == "tool" and not s.capture_output:
                    continue
                text = getter()
                if text:
                    messages.append({"role": role, "content": text})
            for i, msg in enumerate(messages):
                self._set(span, f"llm.input_messages.{i}.message.role", msg["role"])
                self._set(span, f"llm.input_messages.{i}.message.content", msg["content"])
            if messages:
                self._set(span, "input.value", json.dumps(messages, ensure_ascii=False))
                self._set(span, "input.mime_type", "application/json")
        calls = resp.tool_calls
        for j, call in enumerate(calls):
            prefix = f"llm.output_messages.0.message.tool_calls.{j}.tool_call"
            self._set(span, f"{prefix}.id", call["call_id"])
            if call.get("name"):
                self._set(span, f"{prefix}.function.name", call["name"])
            if s.capture_input and isinstance(call.get("arguments"), str):
                self._set(span, f"{prefix}.function.arguments", call["arguments"])
        text = resp.transcript or resp.text
        if calls or (s.capture_output and text):
            self._set(span, "llm.output_messages.0.message.role", "assistant")
        if s.capture_output and text:
            self._set(span, "llm.output_messages.0.message.content", text)
            self._set(span, "output.value", text)
            self._set(span, "output.mime_type", "text/plain")
        elif s.capture_output and s.capture_input and calls:
            shown = [{k: c.get(k) for k in ("call_id", "name", "arguments")} for c in calls]
            self._set(span, "output.value", json.dumps(shown, ensure_ascii=False))
            self._set(span, "output.mime_type", "application/json")

    # ------------------------------------------------------------------ tools

    def _tool_announced(self, call_id: object, name: object, arguments: object, rid: object, now: int) -> None:
        if not isinstance(call_id, str) or not call_id or call_id in self._tools:
            return
        resp = self._response_for(rid)
        if resp is not None and not any(c["call_id"] == call_id for c in resp.tool_calls):
            resp.tool_calls.append(
                {"call_id": call_id, "name": name if isinstance(name, str) else "", "arguments": arguments}
            )
        parent = resp.span if resp is not None and resp.span is not None else self.root
        span = self._child(TOOL_NAME, now, parent=parent)
        self._set(span, FI_SPAN_KIND, "TOOL")
        self._set(span, "gen_ai.tool.call.id", call_id)
        if isinstance(name, str) and name:
            self._set(span, "gen_ai.tool.name", name)
            self._set(span, "tool.name", name)
        if self._settings.capture_input and isinstance(arguments, str):
            self._set(span, "gen_ai.tool.call.arguments", arguments)
            try:
                parsed = json.loads(arguments)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                self._set(span, "input.value", json.dumps(parsed, ensure_ascii=False))
                self._set(span, "input.mime_type", "application/json")
            else:
                # Truncated or invalid JSON stays as the raw string.
                self._set(span, "input.value", arguments)
                self._set(span, "input.mime_type", "text/plain")
        self._tools[call_id] = _Tool(span=span, call_id=call_id, name=name if isinstance(name, str) else None)

    def _client_item(self, item: Dict[str, Any], now: int) -> None:
        itype = item.get("type")
        if itype == "function_call_output":
            call_id = item.get("call_id")
            output = item.get("output")
            if isinstance(output, str):
                self._pending_inputs.append(("tool", lambda o=output: o))
            tool = self._tools.get(call_id) if isinstance(call_id, str) else None
            if tool is not None and tool.span is not None:
                self._tool_result(tool.span, output)
                tool.span.set_status(Status(StatusCode.OK))
                tool.span.end(end_time=now)
                tool.span = None
            elif tool is None:
                # A result for a call this session never announced: visible, not dropped.
                span = self._child(TOOL_NAME, now)
                self._set(span, FI_SPAN_KIND, "TOOL")
                if isinstance(call_id, str):
                    self._set(span, "gen_ai.tool.call.id", call_id)
                self._set(span, "realtime.unmatched", True)
                self._tool_result(span, output)
                span.set_status(Status(StatusCode.ERROR, "unmatched call_id"))
                span.end(end_time=now)
        elif itype == "message" or (itype is None and "role" in item):
            role = item.get("role")
            texts = []
            for part in item.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif isinstance(part, dict) and isinstance(part.get("transcript"), str):
                    texts.append(part["transcript"])
            text = " ".join(texts) if texts else None
            if role in ("user", "system", "developer") and text:
                self._pending_inputs.append(("system" if role == "developer" else role, lambda t=text: t))
            if role == "user" and text:
                self._transcript.append((self._next_seq(), "user", lambda t=text: t))

    def _tool_result(self, span: Any, output: object) -> None:
        if self._settings.capture_output and isinstance(output, str):
            self._set(span, "gen_ai.tool.call.result", output)
            self._set(span, "output.value", output)
            self._set(span, "output.mime_type", "text/plain")

    # ------------------------------------------------------------------ errors

    def _error_event(self, event: Dict[str, Any], now: int) -> None:
        err = event.get("error") if isinstance(event.get("error"), dict) else {}
        span = self._child(ERROR_NAME, now)
        self._set(span, FI_SPAN_KIND, "CHAIN")
        self._set_error_fields(span, err)
        if isinstance(err.get("event_id"), str):
            self._set(span, "realtime.error.client_event_id", err["event_id"])
        span.set_status(Status(StatusCode.ERROR, self._error_description(err)))
        span.end(end_time=now)

    def _set_error_fields(self, span: Any, err: Dict[str, Any]) -> None:
        if isinstance(err.get("type"), str):
            self._set(span, "error.type", err["type"])
        if isinstance(err.get("code"), str):
            self._set(span, "error.code", err["code"])
        if isinstance(err.get("param"), str):
            self._set(span, "realtime.error.param", err["param"])
        if self._settings.capture_output and isinstance(err.get("message"), str):
            self._set(span, "error.message", redact(err["message"], self._secrets))

    def _error_description(self, err: Dict[str, Any]) -> str:
        """Status text. The server's message is content: it only goes in with output capture on."""
        if self._settings.capture_output and isinstance(err.get("message"), str):
            return redact(err["message"], self._secrets)
        parts = [err.get(k) for k in ("type", "code") if isinstance(err.get(k), str)]
        return ": ".join(parts) or "error"

    def _record_exception(self, span: Any, exc: BaseException) -> None:
        """No stack trace, ever: it carries local paths. The message only with output capture on."""
        if span is None:
            return
        name = f"{type(exc).__module__}.{type(exc).__qualname__}"
        attrs = {"exception.type": name}
        description = type(exc).__name__
        if self._settings.capture_output:
            message = redact(str(exc), self._secrets)
            attrs["exception.message"] = message
            description = f"{description}: {message}" if message else description
        span.add_event("exception", attrs)
        span.set_status(Status(StatusCode.ERROR, description))

    # ------------------------------------------------------------------ finish

    def _close_in_flight(self, reason: str, *, error: bool, now: int) -> None:
        for turn in list(self._turns.values()):
            if turn.span is None:
                continue
            if turn.committed_ns is not None:
                # The turn ended; only its transcript never arrived.
                self._set(turn.span, "realtime.transcription", "not_received")
                self._close_turn(turn, end_ns=turn.committed_ns)
            else:
                self._close_turn(turn, end_ns=now, counted=False, reason=reason)
        self._turns = {k: t for k, t in self._turns.items() if t.span is not None}
        open_responses = list(self._responses.values()) + ([self._anon_response] if self._anon_response else [])
        self._responses = {}
        self._anon_response = None
        for resp in open_responses:
            resp.done_ns = now
            self._end_response(resp, reason=reason)
        for tool in self._tools.values():
            if tool.span is not None:
                self._set(tool.span, "realtime.end_reason", reason)
                self._set(tool.span, "realtime.tool_output", "not_sent")
                if error:
                    tool.span.set_status(Status(StatusCode.ERROR, reason))
                tool.span.end(end_time=now)
                tool.span = None

    def _finish_locked(self, reason: str, *, close_code: Optional[int] = None, exc: Optional[BaseException] = None) -> None:
        if self.finished or self.root is None:
            return
        self.finished = True
        self.end_reason = reason
        now = _now()
        error = reason in ERROR_REASONS
        self._release_held()
        self._close_in_flight(reason, error=error, now=now)
        root = self.root
        audio = self._audio_seen or bool(self._output_modalities and "audio" in self._output_modalities)
        self._set(root, FI_SPAN_KIND, "CONVERSATION" if audio else "CHAIN")
        self._set(root, "realtime.end_reason", reason)
        if close_code:
            self._set(root, "realtime.close_code", close_code)
        if self._drops:
            self._set(root, "realtime.socket_drop_count", self._drops)
        instructions = self._client_instructions or self._server_instructions
        if self._settings.capture_input and instructions:
            self._set(root, "input.value", instructions)
            self._set(root, "input.mime_type", "text/plain")
        if audio and reason != "connect_failed":
            self._set(root, "call.duration", round((now - self._start_ns) / 1e9, 3))
            if self._closed_turns:
                self._set(root, "call.total_turns", self._closed_turns)
            if self._assistant_audio_known and self._user_audio_ms > 0 and self._assistant_audio_ms > 0:
                ratio = self._assistant_audio_ms / (self._user_audio_ms + self._assistant_audio_ms)
                self._set(root, "call.talk_ratio", round(ratio, 4))
        if self._settings.capture_conversation:
            rows = []
            for _, role, getter in sorted(self._transcript, key=lambda row: row[0]):
                text = getter()
                if role in ("user", "assistant") and isinstance(text, str) and text.strip():
                    rows.append({"role": role, "content": text})
            if rows:
                blob = json.dumps(rows, ensure_ascii=False)
                self._set(root, "conversation.transcript", blob)
                self._set(root, "fi.conversation.transcript", blob)
                for i, row in enumerate(rows):
                    self._set(root, f"conversation.transcript.{i}.message.role", row["role"])
                    self._set(root, f"conversation.transcript.{i}.message.content", row["content"])
                self._set(root, "output.value", "\n".join(f"{r['role']}: {r['content']}" for r in rows))
                self._set(root, "output.mime_type", "text/plain")
        if exc is not None:
            self._record_exception(root, exc)
        elif error:
            root.set_status(Status(StatusCode.ERROR, reason))
        elif reason != "process_exit":
            root.set_status(Status(StatusCode.OK))
        root.end(end_time=now)
        if self._on_finish is not None:
            try:
                self._on_finish(self)
            except Exception:
                pass
        if reason in FLUSH_REASONS and self._flush is not None:
            try:
                self._flush()
            except Exception:
                logger.debug("traceai-openai-realtime: flush failed", exc_info=True)

    # ------------------------------------------------------------------ helpers

    def _child(self, name: str, start_ns: int, parent: Any = None) -> Any:
        return self._tracer.start_span(
            name,
            context=trace_api.set_span_in_context(parent if parent is not None else self.root),
            start_time=start_ns,
        )

    @staticmethod
    def _set(span: Any, key: str, value: Any) -> None:
        if span is not None and value is not None:
            span.set_attribute(key, value)


def _close_code(exc: BaseException) -> Optional[int]:
    for attr in ("rcvd", "sent"):
        frame = getattr(exc, attr, None)
        code = getattr(frame, "code", None)
        if isinstance(code, int):
            return code
    return None


_SENTINELS = ("Omit", "NotGiven")


def _clean(value: Any) -> Any:
    """Drop the SDK's omit / not-given sentinels that typed helpers can leave in a param dict."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if type(v).__name__ not in _SENTINELS}
    if isinstance(value, list):
        return [_clean(v) for v in value if type(v).__name__ not in _SENTINELS]
    return value


def _as_dict(event: Any) -> Optional[Dict[str, Any]]:
    """A client event as a dict: TypedDict params, SDK models, or raw JSON."""
    if isinstance(event, dict):
        return _clean(event)
    if isinstance(event, (str, bytes, bytearray)):
        try:
            data = json.loads(event)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        return data if isinstance(data, dict) else None
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None
