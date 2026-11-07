"""Turn Hermes Agent plugin-hook payloads into OpenTelemetry spans.

One trace per Hermes turn:

    hermes.turn                 AGENT   pre_llm_call ... on_session_end
      chat <model>              LLM     pre_api_request ... post_api_request / api_request_error
      <tool name>               TOOL    post_tool_call (start = end - duration_ms)

Every turn carries ``session.id`` (the Hermes session id), so the turns of one
``hermes chat`` session group under one Future AGI session.

Hooks can fire on Hermes worker threads, so spans are tracked by the ids the
hooks carry (turn_id, api_request_id, tool_call_id) and parented explicitly,
never through the ambient OpenTelemetry context.

Prompt, response, tool-argument, tool-result and error text is only read
when ``capture_content`` is true. With it off, the only things exported are
ids, model and provider names, token counts, sizes, durations, outcomes and
Hermes' error categories (never the error messages).

The plugin observes only: it does not register on ``pre_tool_call``, which
Hermes treats as a fail-closed policy hook.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode

from ._privacy import private_history, redact

logger = logging.getLogger(__name__)

SPAN_KIND = "gen_ai.span.kind"
OPERATION = "gen_ai.operation.name"
SESSION_ID = "session.id"
CONVERSATION_ID = "gen_ai.conversation.id"
INPUT_VALUE = "input.value"
INPUT_MIME = "input.mime_type"
OUTPUT_VALUE = "output.value"
OUTPUT_MIME = "output.mime_type"
JSON_MIME = "application/json"
TEXT_MIME = "text/plain"

TURN_SPAN_NAME = "hermes.turn"
# Tool statuses Hermes reports on post_tool_call that are not a success.
_FAILED_TOOL_STATUSES = frozenset({"error", "blocked", "timeout", "cancelled"})
# Native reasons are categories, but unknown hosts/hooks may supply arbitrary
# exception text instead. A syntactically valid identifier is not proof that a
# value is safe metadata (it could itself be a private error marker).
_EXIT_CATEGORIES = frozenset({
    "error", "text_response", "keyboard_interrupt", "interpreter_shutdown",
    "repeated_outer_errors", "local_processing_error", "error_near_max_iterations",
    "max_iterations_reached", "context_compression_exhausted", "context_compression_timeout",
    "compaction_handoff_not_actionable", "session_persistence_failed", "guardrail_halt",
    "pending_tool_result", "ollama_runtime_context_too_small", "partial_stream_recovery",
    "fallback_prior_turn_content", "empty_response_exhausted", "interrupted_by_system",
    "interrupted_by_user", "interrupted_during_api_call", "review_input_budget_exhausted",
    "budget_exhausted", "redirect_restart_limit_exceeded", "rebuilt_restart_limit_exceeded",
    "all_retries_exhausted_no_response", "blocked_by_plugin_pre_llm_call",
})
_TRUNCATED = "...[truncated {0} chars]"


@dataclass
class _Turn:
    span: Span
    session_id: str
    children: List[str] = field(default_factory=list)
    pending_output: Optional[str] = None
    provider_failure: Optional[str] = None


@dataclass(frozen=True)
class _Origin:
    context: trace_api.SpanContext
    expires_at: float


def _ns(epoch_seconds: Any) -> Optional[int]:
    """Epoch seconds (Hermes' started_at / ended_at) to OTel nanoseconds."""
    if isinstance(epoch_seconds, bool) or not isinstance(epoch_seconds, (int, float)):
        return None
    if epoch_seconds <= 0:
        return None
    return int(epoch_seconds * 1_000_000_000)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _host(url: Any) -> Optional[str]:
    """Only the host of a provider base URL; a URL can carry credentials."""
    if not isinstance(url, str) or not url:
        return None
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _set(span: Span, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        if value is None or value == "":
            continue
        span.set_attribute(key, value)


class HermesSpanRecorder:
    """Maps the documented Hermes plugin hooks to spans on one tracer.

    Each ``on_*`` method takes the hook's keyword arguments. Methods never raise
    and always return None (never a directive), so a recorder bug cannot change
    what Hermes does.
    """

    def __init__(self, tracer: trace_api.Tracer, *, capture_content: bool = False,
                 max_chars: int = 8000, hide_inputs: bool = False, hide_outputs: bool = False,
                 hide_errors: bool = False, secrets: tuple = (), user_id: Optional[str] = None) -> None:
        self._tracer = tracer
        self._capture = bool(capture_content)
        self._max_chars = max(256, min(20000, int(max_chars)))
        self._hide_inputs, self._hide_outputs, self._hide_errors = hide_inputs, hide_outputs, hide_errors
        self._secrets = tuple(s for s in secrets if s)
        self._user_id = user_id
        self._lock = threading.RLock()
        self._turns: Dict[str, _Turn] = {}
        # open LLM/tool spans: key -> (span, turn key)
        self._children: Dict[str, tuple] = {}
        # session id -> most recent open turn key (subagent parenting, session end)
        self._session_turn: Dict[str, str] = {}
        # Retain only trace context, never ended spans or prompts. A background
        # child can start/finish after its initiating turn has already exported.
        self._subagent_parent: Dict[str, _Origin] = {}
        self._delegations: Dict[tuple, _Origin] = {}
        # api_request_ids whose earlier attempt already ended (retries)
        self._attempted: Dict[str, int] = {}
        # LLM span key -> start time in epoch seconds (time to first chunk)
        self._llm_start: Dict[str, float] = {}
        # Native stream observers carry turn + iteration, not request/attempt ids.
        # Only a first attempt with an unambiguous active request can qualify.
        self._stream_requests: Dict[tuple, str] = {}
        self._first_content: Dict[str, float] = {}

    @property
    def capture_content(self) -> bool:
        return self._capture

    # ------------------------------------------------------------------ helpers

    def _text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        value = redact(value, self._secrets)
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(value)
            text = redact(text, self._secrets)
        if len(text) > self._max_chars:
            text = text[: self._max_chars] + _TRUNCATED.format(len(text) - self._max_chars)
        return text

    def _content(self, span: Span, key: str, mime_key: str, value: Any) -> None:
        if not self._capture or value is None:
            return
        if (key == INPUT_VALUE and self._hide_inputs) or (key == OUTPUT_VALUE and self._hide_outputs):
            return
        text = self._text(value)
        if text is None:
            return
        span.set_attribute(key, text)
        span.set_attribute(mime_key, TEXT_MIME if isinstance(value, str) else JSON_MIME)

    @staticmethod
    def _turn_key(kw: Mapping[str, Any]) -> str:
        turn_id = _str(kw.get("turn_id"))
        if turn_id:
            return turn_id
        return "{0}:{1}".format(_str(kw.get("session_id")), _str(kw.get("task_id")))

    def _retain(self, cache: dict, key: Any, context: trace_api.SpanContext) -> None:
        now = time.monotonic()
        for expired in [k for k, v in cache.items() if v.expires_at <= now]:
            cache.pop(expired, None)
        if len(cache) >= 256 and key not in cache:
            cache.pop(next(iter(cache)))
        cache[key] = _Origin(context, now + 3600)

    def _continuation(self, kw: Mapping[str, Any]) -> Optional[_Origin]:
        # Native turn_context appends the current user row before pre_llm_call.
        # Only its typed display metadata identifies a host-generated completion;
        # never infer provenance from untyped text or old rows in the conversation.
        if kw.get("telemetry_schema_version") != "hermes.observer.v1":
            return None
        history = kw.get("conversation_history")
        row = history[-1] if isinstance(history, list) and history else None
        if (not isinstance(row, Mapping) or row.get("role") != "user"
                or row.get("display_kind") != "async_delegation_complete"):
            return None
        metadata = row.get("display_metadata")
        delegation = metadata.get("delegation_id") if isinstance(metadata, Mapping) else None
        if not isinstance(delegation, str):
            # The pinned CLI's TimelineNotification keeps display_kind but only
            # display_text/category in metadata (unlike the TUI gateway). Extract
            # the handle from the header ONLY after verifying that native type;
            # a human copying this header has no display_kind and cannot attach.
            text = row.get("content")
            match = re.match(r"^\[ASYNC DELEGATION BATCH COMPLETE — (deleg_[A-Za-z0-9_-]{1,128})\]",
                             text[:256]) if isinstance(text, str) else None
            if not match:
                return None
            delegation = match.group(1)
        origin = self._delegations.get((_str(kw.get("session_id")), delegation))
        return origin if origin and origin.expires_at > time.monotonic() else None

    def _remember_delegation(self, kw: Mapping[str, Any]) -> None:
        if (kw.get("telemetry_schema_version") != "hermes.observer.v1"
                or kw.get("tool_name") != "delegate_task" or kw.get("status") in _FAILED_TOOL_STATUSES):
            return
        result = kw.get("result")
        if isinstance(result, str) and len(result) <= 65536:
            try:
                result = json.loads(result)
            except (ValueError, TypeError):
                return
        if not isinstance(result, Mapping) or result.get("status") != "dispatched" or result.get("mode") != "background":
            return
        units = result.get("units")
        entries = [result] + (units[:256] if isinstance(units, list) else [])
        with self._lock:
            turn = self._turns.get(self._turn_key(kw))
            if not turn:
                return
            for entry in entries:
                delegation = entry.get("delegation_id") if isinstance(entry, Mapping) else None
                if isinstance(delegation, str) and 0 < len(delegation) <= 256:
                    self._retain(self._delegations, (turn.session_id, delegation), turn.span.get_span_context())

    def _ensure_turn(self, kw: Mapping[str, Any]) -> _Turn:
        key = self._turn_key(kw)
        session_id = _str(kw.get("session_id"))
        with self._lock:
            turn = self._turns.get(key)
            if turn is not None:
                return turn
            if len(self._turns) >= 256:
                self._end_turn(next(iter(self._turns)), attributes={"hermes.unfinished": True, "hermes.gap": "capacity"})
            continuation = self._continuation(kw)
            origin = continuation or self._subagent_parent.get(session_id)
            parent_session = _str(kw.get("parent_session_id"))
            if origin and origin.expires_at <= time.monotonic():
                origin = None
            parent_key = self._session_turn.get(parent_session) if parent_session else None
            parent = self._turns.get(parent_key) if parent_key else None
            # NonRecordingSpan retains ancestry without reopening/re-exporting
            # the immutable root or repeating its request usage.
            parent_span = trace_api.NonRecordingSpan(origin.context) if origin else parent.span if parent else None
            ctx = trace_api.set_span_in_context(parent_span) if parent_span else context_api.Context()
            span = self._tracer.start_span("hermes.continuation" if continuation else TURN_SPAN_NAME, context=ctx)
            if continuation:
                span.set_attribute("fi.agent.continuation", "async-delegation")
            _set(span, {
                SPAN_KIND: "AGENT",
                OPERATION: "invoke_agent",
                "gen_ai.agent.name": "hermes",
                SESSION_ID: session_id,
                CONVERSATION_ID: session_id,
                "hermes.session_id": session_id,
                "user.id": self._user_id,
                "hermes.turn_id": _str(kw.get("turn_id")),
                "hermes.task_id": _str(kw.get("task_id")),
                "hermes.platform": _str(kw.get("platform")),
                "hermes.model": _str(kw.get("model")),
                "hermes.parent_session_id": parent_session,
            })
            if isinstance(kw.get("is_first_turn"), bool):
                span.set_attribute("hermes.is_first_turn", kw["is_first_turn"])
            turn = _Turn(span=span, session_id=session_id)
            self._turns[key] = turn
            if session_id:
                self._session_turn[session_id] = key
            return turn

    def _start_child(self, key: str, name: str, kw: Mapping[str, Any],
                     start_time: Optional[int] = None) -> Span:
        turn = self._ensure_turn(kw)
        span = self._tracer.start_span(
            name, context=trace_api.set_span_in_context(turn.span), start_time=start_time,
        )
        if turn.session_id:
            span.set_attribute(SESSION_ID, turn.session_id)
        with self._lock:
            stale = self._children.pop(key, None)
            self._children[key] = (span, self._turn_key(kw))
            turn.children.append(key)
        if stale is not None:
            # Same id started twice without an end hook: close the first one.
            stale[0].set_attribute("hermes.unfinished", True)
            stale[0].end()
        return span

    def _pop_child(self, key: str) -> Optional[Span]:
        with self._lock:
            entry = self._children.pop(key, None)
        return entry[0] if entry else None

    def _end_turn(self, key: str, *, attributes: Mapping[str, Any],
                  status: Optional[Status] = None) -> None:
        with self._lock:
            turn = self._turns.pop(key, None)
            if turn is None:
                return
            if self._session_turn.get(turn.session_id) == key:
                del self._session_turn[turn.session_id]
            orphans = [self._children.pop(k)[0] for k in turn.children if k in self._children]
            for child in turn.children:
                if child.startswith("llm:"):
                    self._attempted.pop(child[4:], None)
                    self._llm_start.pop(child, None)
                    self._first_content.pop(child, None)
            self._stream_requests = {k: v for k, v in self._stream_requests.items() if v not in turn.children}
        for span in orphans:
            span.set_attribute("hermes.unfinished", True)
            span.end()
        attributes = dict(attributes)
        if turn.provider_failure and attributes.get("fi.agent.outcome", "incomplete") == "incomplete":
            attributes.update({"fi.agent.outcome": "error", "hermes.failed": True})
            status = Status(StatusCode.ERROR, turn.provider_failure)
        _set(turn.span, {"fi.agent.outcome": "incomplete", **attributes})
        # An incomplete turn may still have a useful partial answer. Error text
        # remains suppressed by hide_errors; incompletion is not itself an error.
        if (attributes.get("fi.agent.outcome") in {"success", "incomplete"}
                and turn.pending_output is not None):
            self._content(turn.span, OUTPUT_VALUE, OUTPUT_MIME, turn.pending_output)
        if status is not None:
            turn.span.set_status(status)
        turn.span.end()

    def _guard(self, hook: str, fn, kw: Mapping[str, Any]) -> None:
        try:
            fn(kw)
        except Exception:  # never let tracing break the agent loop
            logger.debug("traceAI-hermes-agent: %s handler failed", hook, exc_info=True)
        return None

    # -------------------------------------------------------------------- turns

    def on_pre_llm_call(self, **kw: Any) -> None:
        return self._guard("pre_llm_call", self._pre_llm_call, kw)

    def _pre_llm_call(self, kw: Mapping[str, Any]) -> None:
        turn = self._ensure_turn(kw)
        self._content(turn.span, INPUT_VALUE, INPUT_MIME, kw.get("user_message"))

    def on_post_llm_call(self, **kw: Any) -> None:
        return self._guard("post_llm_call", self._post_llm_call, kw)

    def _post_llm_call(self, kw: Mapping[str, Any]) -> None:
        with self._lock:
            turn = self._turns.get(self._turn_key(kw))
        if turn is not None:
            if self._hide_errors:
                if self._capture and not self._hide_outputs:
                    turn.pending_output = self._text(kw.get("assistant_response"))
            else:
                self._content(turn.span, OUTPUT_VALUE, OUTPUT_MIME, kw.get("assistant_response"))

    def on_session_end(self, **kw: Any) -> None:
        return self._guard("on_session_end", self._session_end, kw)

    def _exit_reason(self, raw: Any) -> str:
        """Hermes' exit reason, or only its category unless content capture is on.

        Hermes embeds exception text in some reasons, e.g.
        ``repeated_outer_errors(Error during API call #3: <str(e)>)``.
        """
        reason = _str(raw).strip()
        if (self._capture and not self._hide_errors) or not reason:
            return self._text(reason) or ""
        category = reason.split("(", 1)[0].strip()
        return category if category in _EXIT_CATEGORIES else "other"

    def _session_end(self, kw: Mapping[str, Any]) -> None:
        failed = kw.get("failed") is True
        interrupted = kw.get("interrupted") is True
        reason = self._exit_reason(kw.get("turn_exit_reason") or kw.get("reason"))
        outcome = "error" if failed else "interrupted" if interrupted else (
            "success" if kw.get("completed") is True else "incomplete")
        attributes = {
            "hermes.completed": kw.get("completed") if isinstance(kw.get("completed"), bool) else None,
            "hermes.failed": failed,
            "hermes.interrupted": interrupted,
            "hermes.turn_exit_reason": reason,
            "fi.agent.outcome": outcome,
        }
        # Absence of failure is not completion (e.g. Hermes iteration exhaustion).
        status = (Status(StatusCode.ERROR, reason or "turn failed") if (failed or interrupted)
                  else Status(StatusCode.OK if outcome == "success" else StatusCode.UNSET))
        if _str(kw.get("turn_id")):
            self._end_turn(self._turn_key(kw), attributes=attributes, status=status)
            return
        # CLI/TUI exit shapes carry no turn_id: close whatever is open for the session.
        self._end_session(_str(kw.get("session_id")), attributes=attributes, status=status)

    def on_session_finalize(self, **kw: Any) -> None:
        return self._guard("on_session_finalize", self._session_finalize, kw)

    def _session_finalize(self, kw: Mapping[str, Any]) -> None:
        for session_id in (kw.get("session_id"), kw.get("old_session_id")):
            if session_id:
                self._end_session(_str(session_id), attributes={"hermes.unfinished": True})

    def _end_session(self, session_id: str, *, attributes: Mapping[str, Any],
                     status: Optional[Status] = None) -> None:
        with self._lock:
            keys = [k for k, t in self._turns.items() if t.session_id == session_id]
        for key in keys:
            self._end_turn(key, attributes=attributes, status=status)

    def on_subagent_start(self, **kw: Any) -> None:
        return self._guard("subagent_start", self._subagent_start, kw)

    def _subagent_start(self, kw: Mapping[str, Any]) -> None:
        child = _str(kw.get("child_session_id"))
        parent_turn = _str(kw.get("parent_turn_id"))
        if not child:
            return
        with self._lock:
            parent_key = parent_turn or self._session_turn.get(_str(kw.get("parent_session_id")))
            parent = self._turns.get(parent_key)
            if parent:
                self._retain(self._subagent_parent, child, parent.span.get_span_context())

    def on_subagent_stop(self, **kw: Any) -> None:
        return self._guard("subagent_stop", self._subagent_stop, kw)

    def _subagent_stop(self, kw: Mapping[str, Any]) -> None:
        with self._lock:
            self._subagent_parent.pop(_str(kw.get("child_session_id")), None)

    # ---------------------------------------------------------------- LLM calls

    @staticmethod
    def _llm_key(kw: Mapping[str, Any]) -> str:
        return "llm:" + _str(kw.get("api_request_id"))

    def on_pre_api_request(self, **kw: Any) -> None:
        return self._guard("pre_api_request", self._pre_api_request, kw)

    def _attempt_start(self, kw: Mapping[str, Any]) -> float:
        """Start of this provider attempt, in epoch seconds.

        Hermes sets ``started_at`` once per logical call, before its retry
        loop, so every retry carries the first attempt's start. Only the first
        attempt uses it; a retry starts when its own hook fires.
        """
        started = kw.get("started_at")
        retry = _int(kw.get("retry_count")) or 0
        with self._lock:
            earlier = self._attempted.get(_str(kw.get("api_request_id")), 0)
        if retry == 0 and not earlier and isinstance(started, (int, float)) and started > 0:
            return float(started)
        return time.time()

    def _pre_api_request(self, kw: Mapping[str, Any]) -> None:
        model = _str(kw.get("model"))
        key = self._llm_key(kw)
        start = self._attempt_start(kw)
        span = self._start_child(key, "chat {0}".format(model) if model else "chat",
                                 kw, start_time=_ns(start))
        with self._lock:
            self._llm_start[key] = start
            iteration = _int(kw.get("api_call_count"))
            coordinate = (_str(kw.get("session_id")), _str(kw.get("turn_id")), iteration)
            if all(coordinate) and not kw.get("retry_count") and not self._attempted.get(_str(kw.get("api_request_id"))):
                self._stream_requests[coordinate] = key
        _set(span, {
            SPAN_KIND: "LLM",
            OPERATION: "chat",
            "gen_ai.request.model": model,
            "gen_ai.provider.name": _str(kw.get("provider")),
            "server.address": _host(kw.get("base_url")),
            "hermes.api_mode": _str(kw.get("api_mode")),
            "hermes.api_request_id": _str(kw.get("api_request_id")),
            "hermes.api_call_count": _int(kw.get("api_call_count")),
            "hermes.retry_count": _int(kw.get("retry_count")),
            "hermes.message_count": _int(kw.get("message_count")),
            "hermes.tool_count": _int(kw.get("tool_count")),
            "hermes.approx_input_tokens": _int(kw.get("approx_input_tokens")),
            "hermes.request_char_count": _int(kw.get("request_char_count")),
            "gen_ai.request.max_tokens": _int(kw.get("max_tokens")),
        })
        if self._capture:
            self._content(span, INPUT_VALUE, INPUT_MIME, self._llm_input(kw))

    def _llm_input(self, kw: Mapping[str, Any]) -> Any:
        """The latest messages of the request, newest kept when over the cap.

        The head of the request body is the system prompt, which alone fills
        the cap; it is reported by size only.
        """
        request = kw.get("request")
        body = request.get("body") if isinstance(request, Mapping) else None
        messages = None
        if isinstance(body, Mapping):
            messages = body.get("messages") or body.get("input")
        elif isinstance(request, Mapping):
            messages = request.get("messages")
        if not isinstance(messages, list):
            messages = kw.get("request_messages")
        if not isinstance(messages, list):
            return None
        system_chars = 0
        rest = []
        for message in messages:
            if isinstance(message, Mapping) and message.get("role") in ("system", "developer"):
                system_chars += len(_str(message.get("content")))
            else:
                clean = private_history(message, hide_outputs=self._hide_outputs, hide_errors=self._hide_errors)
                if clean is not None:
                    rest.append(clean)
        if isinstance(body, Mapping) and isinstance(body.get("system"), (str, list)):
            system_chars += len(_str(body.get("system")))
        tail: List[Any] = []
        budget = self._max_chars - 200
        for message in reversed(rest):
            size = len(json.dumps(message, ensure_ascii=False, default=str))
            if tail and size > budget:
                break
            tail.insert(0, message)
            budget -= size
        return {"system_prompt_chars": system_chars, "message_count": len(messages),
                "messages": tail}

    def on_stream_delta(self, **kw: Any) -> None:
        """Observe first nonempty text delivery, never save the streamed content.

        Hermes' first_chunk_at is TTFB, including role-only/lifecycle frames.
        Its asynchronous text observer supplies no source timestamp, so this is
        explicitly callback-observed latency, not exact provider token timing.
        Missing/late observers and retries do not invent a TTFT measurement.
        """
        if kw.get("kind") != "text" or not isinstance(kw.get("delta"), str) or not kw["delta"]:
            return None
        coordinate = (_str(kw.get("session_id")), _str(kw.get("turn_id")), _int(kw.get("iteration")))
        observed = time.time()
        with self._lock:
            key = self._stream_requests.get(coordinate)
            if key in self._children:
                self._first_content.setdefault(key, observed)
        return None

    def on_post_api_request(self, **kw: Any) -> None:
        return self._guard("post_api_request", self._post_api_request, kw)

    def _llm_span_to_end(self, kw: Mapping[str, Any]) -> Span:
        """The open LLM span for this request, or one started late if its pre hook never fired."""
        key = self._llm_key(kw)
        span = self._pop_child(key)
        if span is not None:
            return span
        model = _str(kw.get("model"))
        start = self._attempt_start(kw)
        span = self._start_child(key, "chat {0}".format(model) if model else "chat", kw,
                                 start_time=_ns(start))
        self._pop_child(key)
        with self._lock:
            self._llm_start[key] = start
        _set(span, {SPAN_KIND: "LLM", OPERATION: "chat", "gen_ai.request.model": model,
                    "gen_ai.provider.name": _str(kw.get("provider"))})
        return span

    def _post_api_request(self, kw: Mapping[str, Any]) -> None:
        span = self._llm_span_to_end(kw)
        with self._lock:
            turn = self._turns.get(self._turn_key(kw))
            if turn:
                turn.provider_failure = None  # a later successful retry/fallback recovered
        usage = kw.get("usage") if isinstance(kw.get("usage"), Mapping) else {}
        finish = _str(kw.get("finish_reason"))
        _set(span, {
            "gen_ai.response.model": _str(kw.get("response_model")),
            "fi.usage.scope": "request",
            "fi.usage.contribution": "additive" if usage else "none",
            "fi.agent.usage.additive": bool(usage),
            "fi.agent.usage.cache_in_input": True,
            "fi.agent.usage.reasoning_in_output": True,
            "fi.agent.fidelity": "native-request",
            "gen_ai.response.finish_reasons": [finish] if finish else None,
            # Hermes' prompt_tokens already includes cache reads and writes.
            "gen_ai.usage.input_tokens": _int(usage.get("prompt_tokens")),
            "gen_ai.usage.output_tokens": _int(usage.get("output_tokens")),
            "gen_ai.usage.total_tokens": _int(usage.get("total_tokens")),
            "gen_ai.usage.cache_read_tokens": _int(usage.get("cache_read_tokens")),
            "gen_ai.usage.cache_write_tokens": _int(usage.get("cache_write_tokens")),
            "gen_ai.usage.output_tokens.reasoning": _int(usage.get("reasoning_tokens")),
            "hermes.assistant_tool_call_count": _int(kw.get("assistant_tool_call_count")),
            "hermes.assistant_content_chars": _int(kw.get("assistant_content_chars")),
        })
        with self._lock:
            first_content = self._first_content.pop(self._llm_key(kw), None)
        start = self._finish_attempt(kw)
        first_chunk, ended = kw.get("first_chunk_at"), kw.get("ended_at")
        def measured(value: Any) -> bool:
            return (start is not None and isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and math.isfinite(start) and start <= value
                    and isinstance(ended, (int, float)) and not isinstance(ended, bool)
                    and math.isfinite(ended) and value <= ended)
        if measured(first_chunk):
            span.set_attribute("hermes.time_to_first_chunk_ms", round((first_chunk - start) * 1000))
            span.set_attribute("hermes.first_chunk.source", "post_api_request.first_chunk_at")
        if measured(first_content):
            span.set_attribute("gen_ai.server.time_to_first_token", round((first_content - start) * 1000))
            span.set_attribute("hermes.time_to_first_token.source", "on_stream_delta.text")
            span.set_attribute("hermes.time_to_first_token.timing", "callback-observed")
        if self._capture:
            self._content(span, OUTPUT_VALUE, OUTPUT_MIME, _assistant_output(kw.get("assistant_message")))
        span.set_status(Status(StatusCode.OK))
        span.end(end_time=self._end_time(kw, start))

    def _finish_attempt(self, kw: Mapping[str, Any]) -> Optional[float]:
        """Record that this request id had an attempt; return that attempt's start."""
        key = self._llm_key(kw)
        with self._lock:
            request_id = _str(kw.get("api_request_id"))
            self._attempted[request_id] = self._attempted.get(request_id, 0) + 1
            self._stream_requests = {k: v for k, v in self._stream_requests.items() if v != key}
            self._first_content.pop(key, None)
            return self._llm_start.pop(key, None)

    @staticmethod
    def _end_time(kw: Mapping[str, Any], start: Optional[float]) -> Optional[int]:
        ended = kw.get("ended_at")
        if isinstance(ended, (int, float)) and start is not None and ended >= start:
            return _ns(ended)
        # ended_at is measured from the logical call's start; for a retry that
        # can be earlier than the attempt's own start. Use "now".
        return None if not isinstance(ended, (int, float)) else time.time_ns()

    def on_api_request_error(self, **kw: Any) -> None:
        return self._guard("api_request_error", self._api_request_error, kw)

    def _api_request_error(self, kw: Mapping[str, Any]) -> None:
        span = self._llm_span_to_end(kw)
        reason = _str(kw.get("reason"))
        error = kw.get("error")
        # Hermes sends error={"type": <class or category>, "message": <text>}.
        error_type = _str(error.get("type")) if isinstance(error, Mapping) else (
            type(error).__name__ if isinstance(error, BaseException) else "")
        error_message = error.get("message") if isinstance(error, Mapping) else error
        _set(span, {
            "error.type": reason or error_type,
            "hermes.error_class": error_type if reason else None,
            "http.response.status_code": _int(kw.get("status_code")),
            "hermes.retry_count": _int(kw.get("retry_count")),
            "hermes.max_retries": _int(kw.get("max_retries")),
            "hermes.retryable": kw.get("retryable") if isinstance(kw.get("retryable"), bool) else None,
        })
        # Provider error text can echo the prompt or local paths: it is content.
        description = reason or error_type or "provider request failed"
        if self._capture and not self._hide_errors and error_message:
            description = self._text(str(error_message)) or description
        with self._lock:
            turn = self._turns.get(self._turn_key(kw))
            if turn:
                turn.provider_failure = description
        span.set_status(Status(StatusCode.ERROR, description))
        start = self._finish_attempt(kw)
        span.end(end_time=self._end_time(kw, start))

    # --------------------------------------------------------------- tool calls

    def _tool_key(self, kw: Mapping[str, Any]) -> str:
        call_id = _str(kw.get("tool_call_id"))
        if call_id:
            return "tool:" + call_id
        return "tool:{0}:{1}".format(self._turn_key(kw), _str(kw.get("tool_name")))

    def on_post_tool_call(self, **kw: Any) -> None:
        return self._guard("post_tool_call", self._post_tool_call, kw)

    def _post_tool_call(self, kw: Mapping[str, Any]) -> None:
        # Built from post_tool_call alone, timed by Hermes' duration_ms.
        key = self._tool_key(kw)
        duration = max(0, _int(kw.get("duration_ms")) or 0)
        name = _str(kw.get("tool_name")) or "tool"
        end = time.time_ns()
        span = self._start_child(key, name, kw, start_time=end - duration * 1_000_000)
        self._pop_child(key)
        self._remember_delegation(kw)
        _set(span, {
            SPAN_KIND: "TOOL",
            OPERATION: "execute_tool",
            "tool.name": name,
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": _str(kw.get("tool_call_id")),
            "tool_call.id": _str(kw.get("tool_call_id")),
            "hermes.api_request_id": _str(kw.get("api_request_id")),
        })
        self._content(span, INPUT_VALUE, INPUT_MIME, kw.get("args"))
        status = _str(kw.get("status"))
        error_type = _str(kw.get("error_type"))
        _set(span, {
            "hermes.tool.status": status,
            "hermes.tool.duration_ms": _int(kw.get("duration_ms")),
            "error.type": error_type,
        })
        failed = status in _FAILED_TOOL_STATUSES or bool(error_type)
        if not (failed and self._hide_errors):
            self._content(span, OUTPUT_VALUE, OUTPUT_MIME, kw.get("result"))
        if failed:
            description = error_type or status
            if self._capture and not self._hide_errors and kw.get("error_message"):
                description = self._text(kw.get("error_message")) or description
            span.set_status(Status(StatusCode.ERROR, description))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end(end_time=end)

    # Optional observer callbacks (registered only when this host exposes them).
    # They cannot start a turn or turn aggregate usage into another generation.
    def on_post_compaction(self, **kw: Any) -> None:
        with self._lock:
            key = self._session_turn.get(_str(kw.get("session_id"))) or self._session_turn.get(_str(kw.get("old_session_id")))
            turn = self._turns.get(key) if key else None
            if turn:
                attrs = {"hermes." + k: n for k in ("before_tokens", "after_tokens", "messages_removed")
                         if (n := _int(kw.get(k))) is not None}
                turn.span.add_event("hermes.compaction", attributes=attrs)

    def on_post_approval_response(self, **kw: Any) -> None:
        with self._lock:
            turn = self._turns.get(self._turn_key(kw))
            if turn:
                choice = kw.get("choice")
                allowed = {"allow", "deny", "once", "always", "cancel", "timeout", "approve", "reject"}
                turn.span.add_event("hermes.approval", attributes={"hermes.approval.choice": choice if choice in allowed else "other"})

    # ----------------------------------------------------------------- shutdown

    def end_all(self) -> None:
        """Close every open span (process exit); they are marked unfinished."""
        with self._lock:
            keys = list(self._turns)
        for key in keys:
            self._end_turn(key, attributes={"hermes.unfinished": True})
        with self._lock:
            leftovers = [entry[0] for entry in self._children.values()]
            self._children.clear()
        for span in leftovers:
            span.set_attribute("hermes.unfinished", True)
            span.end()


def _assistant_output(message: Any) -> Any:
    """Text, or the requested tool calls, from Hermes' normalized assistant message."""
    if message is None:
        return None
    content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
    if isinstance(content, str) and content:
        return content
    calls = (message.get("tool_calls") if isinstance(message, Mapping)
             else getattr(message, "tool_calls", None)) or []
    out = []
    for call in calls:
        fn = call.get("function") if isinstance(call, Mapping) else getattr(call, "function", None)
        name = fn.get("name") if isinstance(fn, Mapping) else getattr(fn, "name", None)
        args = fn.get("arguments") if isinstance(fn, Mapping) else getattr(fn, "arguments", None)
        out.append({"name": name, "arguments": args})
    return out or content


HOOKS: Iterable[str] = (
    "pre_llm_call", "post_llm_call", "pre_api_request", "post_api_request",
    "api_request_error", "post_tool_call", "on_session_end",
    "on_session_finalize", "subagent_start", "subagent_stop",
)
