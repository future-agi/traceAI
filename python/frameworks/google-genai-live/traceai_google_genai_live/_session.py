"""One Gemini Live session as one trace.

The root opens when ``connect`` is entered. Turns, tool calls and the reconnect gap are
children of the root. A reconnect that passes the resumption handle this process stored
attaches to the same root. Nothing here opens a socket, retries or reconnects.

Every public ``on_*`` method swallows its own errors: instrumentation must never change
what the customer's code sees.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Status, StatusCode

from ._redact import (
    ContentPolicy,
    _get,
    as_list,
    inline_audio,
    instruction_text,
    pcm_rate,
    roles_of_turns,
    seconds,
    text_of_parts,
    text_of_turns,
    to_json,
    tool_names,
)

if TYPE_CHECKING:  # pragma: no cover
    from ._connect import _Instrumentation

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

FI_SPAN_KIND = "fi.span.kind"
SYSTEM = "google_genai"

ROOT_NAME = "gemini.live.session"
SETUP_NAME = "gemini.live.setup"
CONTEXT_UPDATE_NAME = "gemini.live.context_update"
USER_TURN_NAME = "gemini.live.user_turn"
MODEL_TURN_NAME = "gemini.live.model_turn"
TOOL_NAME = "gemini.live.tool"
TOOL_RESPONSE_NAME = "gemini.live.tool_response"
RECONNECT_NAME = "gemini.live.reconnect"

# usageMetadata fields copied as the server sent them (never summed).
_USAGE_FIELDS = (
    "prompt_token_count",
    "cached_content_token_count",
    "response_token_count",
    "tool_use_prompt_token_count",
    "thoughts_token_count",
    "total_token_count",
)


def _now() -> int:
    return time.time_ns()


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None) or getattr(value, "value", None) or value
    return str(name)


@dataclass(frozen=True)
class _ContextUpdate:
    span: Any
    sequence: int
    system_instruction_changed: bool
    tool_names: Optional[List[str]]  # None means unchanged; [] means cleared


class _Turn:
    __slots__ = ("span", "start_ns", "text", "deferred_end_ns", "boundary", "audio_bytes",
                 "audio_seconds", "audio_rate_known", "calls", "flags", "va_start")

    def __init__(self, span: Any, start_ns: int) -> None:
        self.span = span
        self.start_ns = start_ns
        self.text: List[str] = []
        self.deferred_end_ns: Optional[int] = None
        self.boundary: Optional[str] = None
        self.audio_bytes = 0
        self.audio_seconds = 0.0
        self.audio_rate_known = True
        self.calls: List[Dict[str, Any]] = []
        self.flags: Dict[str, Any] = {}
        self.va_start: Optional[float] = None


class SessionTrace:
    """State for one root span. Lives across reconnects that resume the same handle."""

    def __init__(
        self,
        inst: "_Instrumentation",
        *,
        model: Optional[str],
        audio: bool,
        setup_info: Dict[str, Any],
        session_id: Optional[str],
        user_id: Optional[str],
    ) -> None:
        self.inst = inst
        self.policy: ContentPolicy = inst.policy
        self.model = model
        self.audio = audio
        self.start_ns = _now()
        self.closed = False
        self.detached_ns: Optional[int] = None
        self._expiry: Optional[asyncio.TimerHandle] = None
        self._pending_status: Optional[str] = None
        self.go_away = False
        self.connections = 0
        self.handle: Optional[str] = None  # memory only, never on a span or in a log
        self.handle_resumable = False

        self.user_turn: Optional[_Turn] = None
        self.model_turn: Optional[_Turn] = None
        self.tools: Dict[str, Any] = {}
        self.closed_tool_ids: set = set()
        self.cancelled_ids: set = set()
        self.unconsumed_input: List[str] = []
        self._unconsumed_owner: Optional[_Turn] = None
        self.transcript: List[Dict[str, str]] = []
        self.closed_user_turns = 0
        self.closed_model_turns = 0
        self.realtime_count = 0
        self.realtime_audio_bytes = 0
        self.user_speech_seconds = 0.0
        self.model_audio_seconds = 0.0
        self.model_audio_unmeasured = False
        self._last_receive_error: Optional[str] = None
        self._context_sequence = 0
        self._context_revisions = {"system_instruction": 0, "tools": 0}
        self._context_tool_names = list(setup_info.get("gemini.tool_names", []))

        attrs: Dict[str, Any] = {
            FI_SPAN_KIND: "CONVERSATION" if audio else "CHAIN",
            "gen_ai.system": SYSTEM,
        }
        if model:
            attrs["gen_ai.request.model"] = model
        attrs.update({k: v for k, v in setup_info.items() if v is not None})
        if session_id:
            attrs["session.id"] = session_id
            attrs["gemini.session_id_source"] = "customer"
        else:
            attrs["session.id"] = str(uuid.uuid4())
            attrs["gemini.session_id_source"] = "local"
        if user_id:
            attrs["user.id"] = user_id
        # A voice call must be parentless for the Voice tab to list it. A text session
        # keeps the caller's active span as its parent.
        parent_ctx = context_api.Context() if audio else None
        self.root = inst.tracer.start_span(ROOT_NAME, context=parent_ctx, start_time=self.start_ns,
                                           attributes=attrs)
        self.root_ctx = trace_api.set_span_in_context(self.root)
        self._session_id_from_customer = bool(session_id)

    # ------------------------------------------------------------------ helpers
    def _child(self, name: str, kind: str, start_ns: Optional[int] = None,
               parent: Any = None, attributes: Optional[Dict[str, Any]] = None) -> Any:
        ctx = trace_api.set_span_in_context(parent) if parent is not None else self.root_ctx
        attrs = {FI_SPAN_KIND: kind}
        if attributes:
            attrs.update({k: v for k, v in attributes.items() if v is not None})
        return self.inst.tracer.start_span(name, context=ctx, start_time=start_ns or _now(),
                                           attributes=attrs)

    def _root_event(self, name: str, attributes: Dict[str, Any]) -> None:
        try:
            self.root.add_event(name, {k: v for k, v in attributes.items() if v is not None})
            # Some stores keep event names only; the latest close code also lives on the root.
            if isinstance(attributes.get("gemini.close_code"), int):
                self.root.set_attribute("gemini.close_code", attributes["gemini.close_code"])
        except Exception:
            pass

    def _error_attrs(self, exc: BaseException) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {"exception.type": type(exc).__name__}
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            attrs["gemini.close_code"] = code
        if self.policy.full:
            # Raw error text can echo prompts or local paths, so it is content. Stack
            # traces are never exported.
            attrs["exception.message"] = str(exc)[:2000]
        return attrs

    # ------------------------------------------------------------------ setup
    def start_setup(self, *, system_instruction: Optional[str], path: Optional[str],
                    resumed: bool) -> Any:
        attrs: Dict[str, Any] = {"url.path": path, "gemini.resumed": resumed or None}
        if self.model:
            attrs["gen_ai.request.model"] = self.model
        if system_instruction and self.policy.capture_inputs:
            attrs["input.value"] = system_instruction
            attrs["input.mime_type"] = "text/plain"
        return self._child(SETUP_NAME, "CHAIN", attributes=attrs)

    def end_setup(self, span: Any, session: Any) -> None:
        setup_complete = getattr(session, "setup_complete", None)
        if setup_complete is not None:
            span.set_attribute("gemini.setup_complete", True)
            server_id = getattr(setup_complete, "session_id", None)
            if server_id and not self._session_id_from_customer and self.connections == 0:
                self.root.set_attribute("session.id", str(server_id))
                self.root.set_attribute("gemini.session_id_source", "server")
            span.set_status(Status(StatusCode.OK))
        else:
            span.set_attribute("gemini.setup_complete", False)
            span.set_status(Status(StatusCode.ERROR, "no_setup_complete"))
        span.end()
        self.connections += 1

    def fail_setup(self, span: Any, exc: BaseException, *, resumed: bool) -> None:
        reason = "resume_failed" if resumed else "setup_rejected"
        if isinstance(exc, asyncio.CancelledError):
            reason = "task_cancelled"
        span.set_attributes(self._error_attrs(exc))
        span.set_status(Status(StatusCode.ERROR, reason))
        span.end()
        self._root_event("gemini.setup_failed", self._error_attrs(exc))
        self.close(error=reason)

    # ------------------------------------------------------------------ context updates
    def start_context_update(self, *, system_instruction: Any = None,
                             tools: Any = None) -> _ContextUpdate:
        # Assign order before awaiting the SDK, and snapshot only safe metadata/text.
        # A successful send is not a server acknowledgement that the update was applied.
        self._context_sequence += 1
        sequence = self._context_sequence
        names = tool_names(tools) if tools is not None else None
        attrs: Dict[str, Any] = {
            "gemini.context_update.sequence": sequence,
            "gemini.context_update.system_instruction_changed": system_instruction is not None,
            "gemini.context_update.tools_changed": tools is not None,
        }
        if names is not None:
            attrs["gemini.tool_names"] = names  # [] is an explicit clear, not an omission
        if system_instruction is not None and self.policy.capture_inputs:
            text = instruction_text(system_instruction)
            if text is not None:
                attrs["input.value"] = text
                attrs["input.mime_type"] = "text/plain"
        span = self._child(CONTEXT_UPDATE_NAME, "CHAIN", attributes=attrs)
        return _ContextUpdate(span, sequence, system_instruction is not None, names)

    def end_context_update(self, update: _ContextUpdate, exc: Optional[BaseException] = None) -> None:
        span = update.span
        try:
            span.set_attribute("gemini.context_update.sent", exc is None)
            if exc is not None:
                span.set_attributes(self._error_attrs(exc))
                reason = "task_cancelled" if isinstance(exc, asyncio.CancelledError) else "context_update_failed"
                span.set_status(Status(StatusCode.ERROR, reason))
            else:
                # Concurrent sends may finish out of order. An older completion must not
                # restore stale tools or instructions; track each field independently.
                if (update.system_instruction_changed
                        and update.sequence > self._context_revisions["system_instruction"]):
                    self._context_revisions["system_instruction"] = update.sequence
                if (update.tool_names is not None
                        and update.sequence > self._context_revisions["tools"]):
                    self._context_revisions["tools"] = update.sequence
                    self._context_tool_names = update.tool_names
                    self.root.set_attribute("gemini.tool_names", update.tool_names)
                span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.debug("gemini live: context update not recorded", exc_info=False)
        finally:
            try:
                span.end()
            except Exception:
                pass

    # ------------------------------------------------------------------ client side
    def on_client_content(self, turns: Any, turn_complete: Any, start_ns: int) -> None:
        try:
            now = _now()
            self._finish_deferred_user_turn()
            if self.model_turn is not None:
                # clientContent interrupts generation (E-GEM-2).
                self._close_model_turn(now, interrupted=True, reason="client_content")
            if self.user_turn is not None:
                self._close_user_turn(now, boundary="client_content")
            turn = _Turn(self._child(USER_TURN_NAME, "CHAIN", start_ns, attributes={
                "gemini.input_mode": "client_content",
                "gemini.client_turn_complete": bool(turn_complete) if turn_complete is not None else True,
                "gemini.turn_roles": roles_of_turns(turns) or None,
            }), start_ns)
            text = text_of_turns(turns)
            if text:
                self._add_user_text(turn, text)
            self.user_turn = turn
            if turn_complete is None or bool(turn_complete):
                self._close_user_turn(now, boundary="client_turn_complete")
        except Exception:
            logger.debug("gemini live: client content not recorded", exc_info=False)

    def on_realtime_input(self, kwargs: Dict[str, Any], start_ns: int) -> None:
        try:
            self.realtime_count += 1
            audio = kwargs.get("audio") or kwargs.get("media")
            data = _get(audio, "data") if audio is not None else None
            if isinstance(data, (bytes, bytearray)):
                self.realtime_audio_bytes += len(data)
            text = kwargs.get("text")
            if kwargs.get("activity_start") is not None:
                self._open_user_turn(start_ns, "client_activity")
            if isinstance(text, str) and text:
                turn = self._open_user_turn(start_ns, "realtime_text")
                self._add_user_text(turn, text)
            if kwargs.get("activity_end") is not None and self.user_turn is not None:
                self.user_turn.deferred_end_ns = _now()
                self.user_turn.boundary = "activity_end"
        except Exception:
            logger.debug("gemini live: realtime input not recorded", exc_info=False)

    def on_tool_response(self, function_responses: Any) -> None:
        try:
            now = _now()
            for resp in as_list(function_responses):
                call_id = _get(resp, "id")
                name = _get(resp, "name")
                result = _get(resp, "response")
                if call_id is not None:
                    call_id = str(call_id)
                span = self.tools.get(call_id) if call_id is not None else None
                if span is not None:
                    if self.policy.capture_outputs and result is not None:
                        span.set_attribute("output.value", to_json(result))
                        span.set_attribute("output.mime_type", "application/json")
                    if _get(resp, "will_continue"):
                        span.set_attribute("gemini.will_continue", True)
                        continue
                    span.set_status(Status(StatusCode.OK))
                    span.end(end_time=now)
                    del self.tools[call_id]
                    self.closed_tool_ids.add(call_id)
                    if self.policy.capture_inputs and result is not None:
                        self.unconsumed_input.append(to_json({"tool": name, "result": result}))
                        self._unconsumed_owner = None
                    continue
                attrs: Dict[str, Any] = {"gen_ai.tool.call.id": call_id, "tool.name": name,
                                         "gen_ai.tool.name": name}
                if call_id is not None and (call_id in self.cancelled_ids or call_id in self.closed_tool_ids):
                    # The server cancelled (or already got) this call: the response is not
                    # attached to the tool span (J3).
                    attrs["gemini.ignored"] = True
                    attrs["gemini.ignored_reason"] = (
                        "cancelled" if call_id in self.cancelled_ids else "duplicate")
                    late = self._child(TOOL_RESPONSE_NAME, "TOOL", now, attributes=attrs)
                    late.set_status(Status(StatusCode.OK))
                    late.end(end_time=now)
                else:
                    attrs["gemini.unmatched"] = True
                    late = self._child(TOOL_RESPONSE_NAME, "TOOL", now, attributes=attrs)
                    late.set_status(Status(StatusCode.ERROR, "unmatched_tool_response"))
                    late.end(end_time=now)
        except Exception:
            logger.debug("gemini live: tool response not recorded", exc_info=False)

    # ------------------------------------------------------------------ server side
    def on_server_message(self, msg: Any) -> None:
        try:
            self._on_server_message(msg)
        except Exception:
            logger.debug("gemini live: server message not recorded", exc_info=False)

    def _on_server_message(self, msg: Any) -> None:
        now = _now()
        update = getattr(msg, "session_resumption_update", None)
        if update is not None:
            new_handle = getattr(update, "new_handle", None)
            resumable = getattr(update, "resumable", None)
            if new_handle and resumable is not False:
                self.inst.remember_handle(self, str(new_handle))
            elif resumable is False:
                self.handle_resumable = False

        go_away = getattr(msg, "go_away", None)
        if go_away is not None:
            self.go_away = True
            self.root.set_attribute("gemini.go_away", True)
            time_left = getattr(go_away, "time_left", None)
            if time_left is not None:
                self.root.set_attribute("gemini.time_left", str(time_left))

        activity = getattr(msg, "voice_activity", None)
        if activity is not None:
            kind = _enum_name(getattr(activity, "voice_activity_type", None))
            offset = seconds(getattr(activity, "audio_offset", None))
            if kind == "ACTIVITY_START":
                turn = self._open_user_turn(now, "server_activity", force_new=True)
                turn.va_start = offset
            elif kind == "ACTIVITY_END" and self.user_turn is not None:
                turn = self.user_turn
                if turn.va_start is not None and offset is not None and offset >= turn.va_start:
                    self.user_speech_seconds += offset - turn.va_start
                turn.deferred_end_ns = now
                turn.boundary = "activity_end"

        vad = getattr(msg, "voice_activity_detection_signal", None)
        if vad is not None:
            kind = _enum_name(getattr(vad, "vad_signal_type", None))
            if kind.endswith("_SOS"):
                self._open_user_turn(now, "server_vad", force_new=True)
            elif kind.endswith("_EOS") and self.user_turn is not None:
                self.user_turn.deferred_end_ns = now
                self.user_turn.boundary = "activity_end"

        content = getattr(msg, "server_content", None)
        if content is not None:
            transcription = getattr(content, "input_transcription", None)
            text = getattr(transcription, "text", None) if transcription is not None else None
            if text:
                if self.user_turn is not None:
                    self._add_user_text(self.user_turn, text)
                else:
                    turn = self._open_user_turn(now, "input_transcription")
                    self._add_user_text(turn, text)

            model_turn = getattr(content, "model_turn", None)
            parts = as_list(getattr(model_turn, "parts", None)) if model_turn is not None else []
            out_tr = getattr(content, "output_transcription", None)
            out_text = getattr(out_tr, "text", None) if out_tr is not None else None
            if parts or out_text:
                turn = self._open_model_turn(now)
                for size, mime in inline_audio(parts):
                    turn.audio_bytes += size
                    rate = pcm_rate(mime)
                    if rate:
                        turn.audio_seconds += size / (rate * 2.0)
                    elif size:
                        turn.audio_rate_known = False
                thoughts = sum(1 for p in parts if _get(p, "thought"))
                if thoughts:
                    turn.flags["gemini.thought_parts"] = turn.flags.get("gemini.thought_parts", 0) + thoughts
                if out_text:
                    turn.flags["_has_transcription"] = True
                    turn.text.append(out_text)
                elif not turn.flags.get("_has_transcription"):
                    part_text = text_of_parts(parts)
                    if part_text:
                        turn.text.append(part_text)

            if getattr(content, "generation_complete", None) and self.model_turn is not None:
                self.model_turn.flags["gemini.generation_complete"] = True
            reason = getattr(content, "turn_complete_reason", None)
            if reason is not None and self.model_turn is not None:
                self.model_turn.flags["gemini.turn_complete_reason"] = _enum_name(reason)
            if getattr(content, "interrupted", None):
                if self.model_turn is not None:
                    self._close_model_turn(now, interrupted=True, reason="interrupted")

        tool_call = getattr(msg, "tool_call", None)
        if tool_call is not None:
            self._finish_deferred_user_turn()
            calls = as_list(getattr(tool_call, "function_calls", None))
            if calls:
                turn = self._open_model_turn(now, opened_by="tool_call")
                for call in calls:
                    self._open_tool(turn, call, now)

        cancellation = getattr(msg, "tool_call_cancellation", None)
        if cancellation is not None:
            for call_id in as_list(getattr(cancellation, "ids", None)):
                call_id = str(call_id)
                self.cancelled_ids.add(call_id)
                span = self.tools.pop(call_id, None)
                if span is not None:
                    span.set_attribute("gemini.cancelled", True)
                    span.set_status(Status(StatusCode.OK))
                    span.end(end_time=now)
                    self.closed_tool_ids.add(call_id)

        usage = getattr(msg, "usage_metadata", None)
        if usage is not None:
            self._record_usage(usage)

        if content is not None and getattr(content, "turn_complete", None):
            if self.user_turn is not None:
                self._close_user_turn(now, boundary=self.user_turn.boundary or "turn_complete")
            if self.model_turn is not None:
                self.model_turn.flags["gemini.turn_complete"] = True
                self._close_model_turn(now)

    def _record_usage(self, usage: Any) -> None:
        raw = {}
        for field in _USAGE_FIELDS:
            value = getattr(usage, field, None)
            if isinstance(value, int):
                raw["gemini.usage." + field] = value
        for detail_field in ("prompt_tokens_details", "response_tokens_details"):
            for detail in as_list(getattr(usage, detail_field, None)):
                modality = _enum_name(getattr(detail, "modality", None)).lower()
                count = getattr(detail, "token_count", None)
                if isinstance(count, int):
                    raw[f"gemini.usage.{detail_field}.{modality}"] = count
        if not raw:
            return
        target = self.model_turn
        if target is None:
            # No open model turn: keep the server's numbers on the root as an event, so
            # two messages stay two values and nothing is summed.
            self._root_event("gemini.usage_metadata", raw)
            return
        target.span.add_event("gemini.usage_metadata", raw)
        # The latest message wins on the turn. Copied, never added.
        target.span.set_attributes(raw)
        mapping = {
            "gemini.usage.prompt_token_count": "llm.token_count.prompt",
            "gemini.usage.response_token_count": "llm.token_count.completion",
            "gemini.usage.total_token_count": "llm.token_count.total",
        }
        for src, dst in mapping.items():
            if src in raw:
                target.span.set_attribute(dst, raw[src])

    # ------------------------------------------------------------------ turns
    def _open_user_turn(self, start_ns: int, how: str, force_new: bool = False) -> _Turn:
        if self.user_turn is not None:
            if not force_new and self.user_turn.deferred_end_ns is None:
                return self.user_turn
            self._close_user_turn(start_ns, boundary=self.user_turn.boundary or "new_activity")
        span = self._child(USER_TURN_NAME, "CHAIN", start_ns, attributes={
            "gemini.input_mode": "realtime_input" if how != "client_content" else how,
            "gemini.turn_start": how,
        })
        self.user_turn = _Turn(span, start_ns)
        return self.user_turn

    def _add_user_text(self, turn: _Turn, text: str) -> None:
        turn.text.append(text)
        # Transcription arrives in chunks: keep one prompt entry per user turn.
        if self.unconsumed_input and self._unconsumed_owner is turn:
            self.unconsumed_input[-1] += text
        else:
            self.unconsumed_input.append(text)
            self._unconsumed_owner = turn

    def _finish_deferred_user_turn(self) -> None:
        turn = self.user_turn
        if turn is not None and turn.deferred_end_ns is not None:
            self._close_user_turn(turn.deferred_end_ns, boundary=turn.boundary or "activity_end")

    def _close_user_turn(self, end_ns: int, *, boundary: str, error: Optional[str] = None) -> None:
        turn = self.user_turn
        if turn is None:
            return
        self.user_turn = None
        span = turn.span
        span.set_attribute("gemini.turn_boundary", boundary)
        text = "".join(turn.text)
        if text and self.policy.capture_inputs:
            span.set_attribute("input.value", text)
            span.set_attribute("input.mime_type", "text/plain")
        if error:
            span.set_attribute("gemini.close_reason", error)
            span.set_status(Status(StatusCode.ERROR, error))
        else:
            span.set_status(Status(StatusCode.OK))
            self.closed_user_turns += 1
        span.end(end_time=max(end_ns, turn.start_ns))
        if text:
            row = {"role": "user", "content": text}
            self.transcript.append(row)

    def _open_model_turn(self, start_ns: int, opened_by: str = "server_content") -> _Turn:
        if self.model_turn is not None:
            return self.model_turn
        self._finish_deferred_user_turn()
        attrs: Dict[str, Any] = {
            "gen_ai.system": SYSTEM,
            "gemini.turn_start": opened_by,
            "gemini.context.system_instruction_revision": self._context_revisions["system_instruction"],
            "gemini.context.tools_revision": self._context_revisions["tools"],
            "gemini.tool_names": self._context_tool_names,
        }
        if self.model:
            attrs["llm.model_name"] = self.model
            attrs["gen_ai.request.model"] = self.model
        if self.unconsumed_input and self.policy.capture_inputs:
            attrs["input.value"] = "\n".join(self.unconsumed_input)
            attrs["input.mime_type"] = "text/plain"
        self.unconsumed_input = []
        self._unconsumed_owner = None
        self.model_turn = _Turn(self._child(MODEL_TURN_NAME, "LLM", start_ns, attributes=attrs), start_ns)
        return self.model_turn

    def _close_model_turn(self, end_ns: int, *, interrupted: bool = False,
                          reason: Optional[str] = None, error: Optional[str] = None) -> None:
        turn = self.model_turn
        if turn is None:
            return
        self.model_turn = None
        span = turn.span
        for key, value in turn.flags.items():
            if not key.startswith("_"):
                span.set_attribute(key, value)
        if turn.audio_bytes:
            span.set_attribute("gemini.output_audio_bytes", turn.audio_bytes)
            if turn.audio_rate_known:
                span.set_attribute("gemini.output_audio_seconds", round(turn.audio_seconds, 3))
                self.model_audio_seconds += turn.audio_seconds
            else:
                self.model_audio_unmeasured = True
        text = "".join(turn.text)
        if self.policy.capture_outputs:
            if text:
                span.set_attribute("output.value", text)
                span.set_attribute("output.mime_type", "text/plain")
            elif turn.calls:
                span.set_attribute("output.value", json.dumps(turn.calls, ensure_ascii=False, default=str))
                span.set_attribute("output.mime_type", "application/json")
        if interrupted:
            span.set_attribute("gemini.interrupted", True)
        if reason:
            span.set_attribute("gemini.close_reason", reason)
        if error:
            span.set_status(Status(StatusCode.ERROR, error))
        else:
            span.set_status(Status(StatusCode.OK))
            self.closed_model_turns += 1
        # Tool spans opened by this turn keep running: the turn is the model's request,
        # the tool runs on the customer's side and closes on its own toolResponse.
        span.end(end_time=max(end_ns, turn.start_ns))
        if text:
            self.transcript.append({"role": "assistant", "content": text})

    def _open_tool(self, turn: _Turn, call: Any, now: int) -> None:
        call_id = _get(call, "id")
        call_id = str(call_id) if call_id is not None else None
        name = _get(call, "name")
        if call_id is not None and (call_id in self.tools or call_id in self.closed_tool_ids):
            return  # server-supplied id seen before: no second span
        args = _get(call, "args")
        attrs: Dict[str, Any] = {
            "tool.name": name,
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": call_id,
        }
        if self.policy.capture_inputs and args is not None:
            attrs["input.value"] = to_json(args)
            attrs["input.mime_type"] = "application/json"
        span = self._child(TOOL_NAME, "TOOL", now, parent=turn.span, attributes=attrs)
        key = call_id if call_id is not None else f"_noid_{len(self.tools)}"
        self.tools[key] = span
        entry: Dict[str, Any] = {"name": name}
        if args is not None:
            entry["args"] = json.loads(to_json(args))
        turn.calls.append(entry)

    # ------------------------------------------------------------------ errors and exit
    def on_receive_error(self, exc: BaseException) -> None:
        try:
            reason = "socket_closed" if _is_socket_close(exc) else "receive_error"
            self._root_event("gemini.receive_error", self._error_attrs(exc))
            self._close_open(_now(), reason)
            self._last_receive_error = reason
        except Exception:
            logger.debug("gemini live: receive error not recorded", exc_info=False)

    def _close_open(self, now: int, reason: str) -> None:
        if self.user_turn is not None:
            if self.user_turn.deferred_end_ns is not None:
                self._finish_deferred_user_turn()
            else:
                self._close_user_turn(now, boundary="connection_end", error=reason)
        if self.model_turn is not None:
            self._close_model_turn(now, error=reason, reason=reason)
        for call_id, span in list(self.tools.items()):
            span.set_attribute("gemini.close_reason", reason)
            span.set_status(Status(StatusCode.ERROR, reason))
            span.end(end_time=now)
            self.closed_tool_ids.add(call_id)
        self.tools.clear()

    def _end_open_clean(self, now: int) -> None:
        """Clean exit: whatever is still open simply ends here, without an error status."""
        if self.user_turn is not None:
            self._finish_deferred_user_turn()
            if self.user_turn is not None:
                self._close_user_turn(now, boundary="connection_end")
        if self.model_turn is not None:
            self._close_model_turn(now, reason="connection_end")
        for call_id, span in list(self.tools.items()):
            span.set_attribute("gemini.unanswered", True)
            span.end(end_time=now)
            self.closed_tool_ids.add(call_id)
        self.tools.clear()

    def on_connection_exit(self, exc: Optional[BaseException]) -> None:
        """The customer's ``async with connect(...)`` block ended."""
        try:
            now = _now()
            if exc is None:
                reason = getattr(self, "_last_receive_error", None)
            elif isinstance(exc, asyncio.CancelledError):
                reason = "task_cancelled"
            elif _is_socket_close(exc):
                reason = "socket_closed"
            else:
                reason = getattr(self, "_last_receive_error", None) or "exception"
            self._last_receive_error = None
            if exc is not None and reason != "task_cancelled":
                self._root_event("gemini.connection_error", self._error_attrs(exc))
            if reason:
                self._close_open(now, reason)
            else:
                self._end_open_clean(now)
            resumable = self.handle is not None and self.handle_resumable
            if reason != "task_cancelled" and resumable and (self.go_away or reason):
                # The customer may reconnect with the stored handle; keep the root open.
                self.detach(now, reason)
                return
            self.close(error=reason, end_ns=now)
        except Exception:
            logger.debug("gemini live: connection exit not recorded", exc_info=False)

    def detach(self, now: int, reason: Optional[str]) -> None:
        self.detached_ns = now
        self._pending_status = reason
        try:
            loop = asyncio.get_running_loop()
            self._expiry = loop.call_later(self.inst.resume_window_seconds, self.expire)
        except RuntimeError:
            self._expiry = None

    def expire(self) -> None:
        if self.closed or self.detached_ns is None:
            return
        self.close(error=self._pending_status, end_ns=self.detached_ns)

    def start_reconnect(self) -> Any:
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        gap_start = self.detached_ns or _now()
        span = self._child(RECONNECT_NAME, "CHAIN", gap_start, attributes={
            "gemini.reconnect.after_go_away": self.go_away or None,
            "gemini.reconnect.previous_close_reason": self._pending_status,
        })
        self.detached_ns = None
        self._pending_status = None
        self.go_away = False
        self.handle_resumable = False
        return span

    def end_reconnect(self, span: Any, ok: bool, exc: Optional[BaseException] = None) -> None:
        now = _now()
        start = getattr(span, "start_time", None)
        if isinstance(start, int):
            span.set_attribute("gemini.reconnect.gap_ms", round((now - start) / 1e6, 3))
        if ok:
            span.set_status(Status(StatusCode.OK))
            self.root.set_attribute("gemini.reconnect_count", max(self.connections - 1, 1))
        else:
            if exc is not None:
                span.set_attributes(self._error_attrs(exc))
            span.set_status(Status(StatusCode.ERROR, "resume_failed"))
        span.end(end_time=now)

    def close(self, *, error: Optional[str] = None, end_ns: Optional[int] = None) -> None:
        if self.closed:
            return
        self.closed = True
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        end_ns = end_ns or _now()
        try:
            if error:
                self._close_open(end_ns, error)
            else:
                self._end_open_clean(end_ns)
        except Exception:
            pass
        root = self.root
        try:
            root.set_attribute("gemini.realtime_input_count", self.realtime_count)
            if self.realtime_audio_bytes:
                root.set_attribute("gemini.realtime_audio_bytes", self.realtime_audio_bytes)
            if self.audio:
                root.set_attribute("call.duration", round((end_ns - self.start_ns) / 1e9, 3))
                turns = self.closed_user_turns + self.closed_model_turns
                if turns:
                    root.set_attribute("call.total_turns", turns)
                if (self.user_speech_seconds > 0 and self.model_audio_seconds > 0
                        and not self.model_audio_unmeasured):
                    # Agent talk time over user talk time, as the platform's Retell
                    # normaliser defines it. Omitted unless both sides were measured.
                    root.set_attribute("call.talk_ratio",
                                       round(self.model_audio_seconds / self.user_speech_seconds, 4))
                if self.policy.full:
                    rows = [r for r in self.transcript if r.get("role") and r.get("content")]
                    if rows:
                        rendered = json.dumps(rows, ensure_ascii=False)
                        root.set_attribute("conversation.transcript", rendered)
                        root.set_attribute("fi.conversation.transcript", rendered)
                        for i, row in enumerate(rows):
                            root.set_attribute(f"conversation.transcript.{i}.message.role", row["role"])
                            root.set_attribute(f"conversation.transcript.{i}.message.content", row["content"])
            if error:
                root.set_attribute("gemini.close_reason", error)
                root.set_status(Status(StatusCode.ERROR, error))
            else:
                root.set_status(Status(StatusCode.OK))
        finally:
            root.end(end_time=max(end_ns, self.start_ns))
            self.inst.forget(self)
            self.inst.flush()


def _is_socket_close(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 1000 <= code <= 4999:
        return True
    try:
        from websockets.exceptions import ConnectionClosed
    except Exception:  # pragma: no cover
        return False
    return isinstance(exc, ConnectionClosed)
