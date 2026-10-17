"""Map one ElevenLabs ``otlp_traces`` object onto Future AGI's Voice-tab attribute names.

Input is the ``otlp_traces`` object ElevenLabs puts in the
``post_call_transcription_otel`` webhook and in
``GET /v1/convai/conversations/{id}?format=opentelemetry``. Output is an OTLP/HTTP
JSON export request for the Future AGI collector.

What is kept as ElevenLabs sent it: ``traceId``, ``spanId``, ``parentSpanId``,
span names (including names this module does not know), start and end times,
status codes, the scope.

What is added:

* the parentless ``elevenlabs.conversation`` root gets ``fi.span.kind=CONVERSATION``
  (the Voice-tab listing predicate), ``gen_ai.system=elevenlabs``, ``call.duration``
  and ``call.total_turns`` when they can be computed from the payload;
* every span gets ``session.id`` = the conversation id;
* tool spans get ``fi.span.kind=TOOL`` and ``tool.name``; agent turns ElevenLabs
  marks with ``elevenlabs.producing_llm`` get ``fi.span.kind=LLM``, the model and the
  token counts from ``elevenlabs.llm_usage.*``; other turns get ``CHAIN``;
* the conversation content: the transcript (``conversation.transcript``,
  ``fi.conversation.transcript``, flattened rows), ``input.value`` / ``output.value`` on
  the root, turn and tool spans, and ``user.id`` when ElevenLabs supplied one.

Content is captured by default and follows the standard traceAI ``TraceConfig``
switches (``FI_HIDE_INPUTS``, ``FI_HIDE_OUTPUTS``, ...); what is never exported in any
mode is listed in :mod:`traceai_elevenlabs_agents.privacy`.

Signed media URLs are always dropped (they expire in about 15 minutes and this
forwarder stores no recordings).
"""

from __future__ import annotations

import ast
import copy
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from fi_instrumentation import TraceConfig

from . import privacy
from .privacy import INPUT, OUTPUT, Capture

ROOT_SPAN_NAME = "elevenlabs.conversation"
USER_TURN_SPAN_NAME = "elevenlabs.recv.user_transcript"
AGENT_TURN_SPAN_NAME = "elevenlabs.recv.agent_response"
TOOL_SPAN_PREFIX = "elevenlabs.tool."

ATTR_CONVERSATION_ID = "elevenlabs.conversation_id"
ATTR_AGENT_ID = "elevenlabs.agent_id"
ATTR_USER_TEXT = "elevenlabs.user.text"
ATTR_AGENT_TEXT = "elevenlabs.agent.text"
ATTR_TOOL_PARAMS = "elevenlabs.tool.params"
ATTR_TOOL_RESULT = "elevenlabs.tool.result"
ATTR_PRODUCING_LLM = "elevenlabs.producing_llm"
ATTR_LLM_USAGE_PREFIX = "elevenlabs.llm_usage."
ATTR_TURN_INDEX = "elevenlabs.turn.index"
ATTR_TIME_IN_CALL = "time_in_call_secs"

# Set by this module or by the platform, never accepted from the payload.
_RESERVED_RESOURCE_KEYS = frozenset({"project_name", "project_type", "project_version_name", "project_version_id"})
_RESERVED_RESOURCE_PREFIXES = ("fi.",)

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX16 = re.compile(r"^[0-9a-fA-F]{16}$")


class MappingInputError(ValueError):
    """The payload is not a usable trace. Maps to HTTP 400; ``code`` is stable."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


# --------------------------------------------------------------------------- OTLP JSON helpers


def any_value_to_python(value: Mapping[str, Any]) -> Any:
    """Decode an OTLP JSON ``AnyValue``."""
    if not isinstance(value, Mapping):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "arrayValue" in value:
        return [any_value_to_python(v) for v in (value["arrayValue"] or {}).get("values", [])]
    if "kvlistValue" in value:
        return {
            kv.get("key", ""): any_value_to_python(kv.get("value", {}))
            for kv in (value["kvlistValue"] or {}).get("values", [])
        }
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def python_to_any_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def attributes_to_dict(attributes: Optional[Iterable[Mapping[str, Any]]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for attribute in attributes or []:
        if isinstance(attribute, Mapping) and "key" in attribute:
            out[str(attribute["key"])] = any_value_to_python(attribute.get("value", {}))
    return out


def _set_attr(attributes: List[Dict[str, Any]], key: str, value: Any) -> None:
    attributes[:] = [a for a in attributes if a.get("key") != key]
    attributes.append({"key": key, "value": python_to_any_value(value)})


def _set_masked(attributes: List[Dict[str, Any]], capture: Capture, key: str, value: Any) -> None:
    """Set an attribute through ``TraceConfig.mask``, as ``FITracer`` does."""
    value = capture.mask(key, value)
    if value is None or value == "":
        return
    _set_attr(attributes, key, value)


def _parse_llm_usage(raw: Any) -> Optional[Dict[str, float]]:
    """``elevenlabs.llm_usage.<model>.<kind>`` values look like ``{'tokens': 12, 'price': 0.0001}``."""
    parsed: Any = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            try:
                parsed = ast.literal_eval(raw)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                return None
    if not isinstance(parsed, dict):
        return None
    out: Dict[str, float] = {}
    for key, number in parsed.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return None
        if isinstance(number, float) and not math.isfinite(number):
            return None
        out[str(key)] = number
    return out


def _keep_attribute(key: str, value_in: Mapping[str, Any], capture: Capture) -> Optional[Dict[str, Any]]:
    """One payload attribute after both privacy layers, or ``None`` when it is dropped."""
    value = any_value_to_python(value_in)
    if privacy.contains_signed_url(value):
        return None
    kind = privacy.classify(key, value, parse_llm_usage=_parse_llm_usage)
    if not capture.allows(kind):
        return None
    if kind == privacy.SAFE:
        cleaned = copy.deepcopy(dict(value_in))
        if isinstance(value, str):
            cleaned = {"stringValue": privacy.scrub_identity(value)}
        return {"key": key, "value": cleaned}
    cleaned = privacy.scrub_any_value(value_in, text=privacy.is_conversation_text(key))
    if cleaned is None:
        return None
    plain = any_value_to_python(cleaned)
    masked = capture.mask(key, plain)
    if masked is None or masked == "":
        return None
    if masked != plain:  # REDACTED_VALUE, or FI_PII_REDACTION rewrote the text
        if isinstance(masked, list):
            cleaned = {"arrayValue": {"values": [python_to_any_value(v) for v in masked]}}
        elif not isinstance(masked, dict):
            cleaned = python_to_any_value(masked)
    return {"key": key, "value": cleaned}


def _filter_attributes(attributes: Optional[Iterable[Mapping[str, Any]]], capture: Capture) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for attribute in attributes or []:
        if not isinstance(attribute, Mapping) or "key" not in attribute:
            continue
        out = _keep_attribute(str(attribute["key"]), attribute.get("value", {}), capture)
        if out is not None:
            kept.append(out)
    return kept


def _filter_resource(attributes: Optional[Iterable[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    """Resource attributes: an allowlist in every mode (no host, process or OS details)."""
    kept: List[Dict[str, Any]] = []
    for attribute in attributes or []:
        if not isinstance(attribute, Mapping) or "key" not in attribute:
            continue
        key = str(attribute["key"])
        value = any_value_to_python(attribute.get("value", {}))
        if key not in privacy.RESOURCE_ALLOWLIST or not isinstance(value, (str, int, float, bool)):
            continue
        if privacy.contains_signed_url(value):
            continue
        if isinstance(value, str):
            kept.append({"key": key, "value": {"stringValue": privacy.scrub_identity(value)}})
        else:
            kept.append({"key": key, "value": copy.deepcopy(attribute.get("value", {}))})
    return kept


def _nanos(span: Mapping[str, Any], key: str) -> Optional[int]:
    raw = span.get(key)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parent(span: Mapping[str, Any]) -> str:
    return str(span.get("parentSpanId") or "")


# --------------------------------------------------------------------------- mapping


def map_otlp_traces(
    otlp_traces: Any,
    *,
    conversation_id: Optional[str],
    project_name: str,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_config: Optional[TraceConfig] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return ``(export_request, summary)``.

    ``trace_config`` is the standard traceAI privacy switch; ``None`` reads the
    ``FI_HIDE_*`` environment variables, as ``TraceConfig()`` does everywhere.

    Raises :class:`MappingInputError` for a payload that is not a trace.
    Any other exception is a bug in this module and must not be exported.
    """
    capture = Capture.from_trace_config(trace_config)
    if not project_name:
        raise ValueError("project_name is required")
    if not isinstance(otlp_traces, Mapping):
        raise MappingInputError("missing_resource_spans", "otlp_traces is not an object")
    resource_spans = otlp_traces.get("resourceSpans")
    if not isinstance(resource_spans, list) or not resource_spans:
        raise MappingInputError("missing_resource_spans", "otlp_traces.resourceSpans is missing or empty")

    # Collect every span first: the root and the turn order need the whole trace.
    all_spans: List[Dict[str, Any]] = []
    resource_conversation_id = None
    for resource_span in resource_spans:
        if not isinstance(resource_span, Mapping):
            raise MappingInputError("invalid_resource_spans", "resourceSpans entry is not an object")
        resource_attrs = attributes_to_dict((resource_span.get("resource") or {}).get("attributes"))
        resource_conversation_id = resource_conversation_id or resource_attrs.get(ATTR_CONVERSATION_ID)
        for scope_span in resource_span.get("scopeSpans") or []:
            if not isinstance(scope_span, Mapping):
                raise MappingInputError("invalid_resource_spans", "scopeSpans entry is not an object")
            for span in scope_span.get("spans") or []:
                if not isinstance(span, Mapping):
                    raise MappingInputError("invalid_span", "span is not an object")
                if not _HEX32.match(str(span.get("traceId") or "")):
                    raise MappingInputError("invalid_span", "span traceId is not 32 hex characters")
                if not _HEX16.match(str(span.get("spanId") or "")):
                    raise MappingInputError("invalid_span", "span spanId is not 16 hex characters")
                parent = _parent(span)
                if parent and not _HEX16.match(parent):
                    raise MappingInputError("invalid_span", "span parentSpanId is not 16 hex characters")
                all_spans.append(span)
    if not all_spans:
        raise MappingInputError("missing_resource_spans", "otlp_traces has no spans")

    root_attrs_in: Dict[str, Any] = {}
    roots = [s for s in all_spans if not _parent(s)]
    root = next((s for s in roots if s.get("name") == ROOT_SPAN_NAME), None)
    if root is None and len(roots) == 1:
        root = roots[0]
    if root is not None:
        root_attrs_in = attributes_to_dict(root.get("attributes"))

    session_id = root_attrs_in.get(ATTR_CONVERSATION_ID) or resource_conversation_id or conversation_id
    if not session_id or not isinstance(session_id, str):
        raise MappingInputError("missing_conversation_id", "no conversation id on the payload")

    # Turns in transcript order: elevenlabs.turn.index when every turn has one,
    # else start time, else payload order (ElevenLabs timestamps are whole seconds).
    turns: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
    for position, span in enumerate(all_spans):
        if span.get("name") in (USER_TURN_SPAN_NAME, AGENT_TURN_SPAN_NAME):
            turns.append((position, span, attributes_to_dict(span.get("attributes"))))
    if turns and all(isinstance(a.get(ATTR_TURN_INDEX), int) for _, _, a in turns):
        turns.sort(key=lambda t: (t[2][ATTR_TURN_INDEX], t[0]))
    else:
        turns.sort(key=lambda t: (_nanos(t[1], "startTimeUnixNano") or 0, t[0]))

    root_start = _nanos(root, "startTimeUnixNano") if root is not None else None
    transcript: List[Dict[str, Any]] = []
    last_user_text_before: Dict[str, str] = {}
    last_user_text: Optional[str] = None
    for _, span, attrs in turns:
        is_user = span.get("name") == USER_TURN_SPAN_NAME
        text = privacy.conversation_text(attrs.get(ATTR_USER_TEXT if is_user else ATTR_AGENT_TEXT))
        if not is_user and last_user_text:
            last_user_text_before[str(span.get("spanId"))] = last_user_text
        if not isinstance(text, str) or not text.strip():
            continue
        if is_user:
            last_user_text = text
        row: Dict[str, Any] = {"role": "user" if is_user else "assistant", "content": text}
        start = _nanos(span, "startTimeUnixNano")
        if root_start is not None and start is not None and start >= root_start:
            row["time"] = str((start - root_start) // 1_000_000_000)
        transcript.append(row)

    out_resource_spans: List[Dict[str, Any]] = []
    span_count = 0
    trace_ids = set()
    for resource_span in resource_spans:
        resource_in = resource_span.get("resource") or {}
        resource_attributes = [
            a
            for a in _filter_resource(resource_in.get("attributes"))
            if a["key"] not in _RESERVED_RESOURCE_KEYS and not a["key"].startswith(_RESERVED_RESOURCE_PREFIXES)
        ]
        _set_attr(resource_attributes, "project_name", project_name)
        _set_attr(resource_attributes, "project_type", "observe")
        out_scopes: List[Dict[str, Any]] = []
        for scope_span in resource_span.get("scopeSpans") or []:
            out_spans = [
                _map_span(
                    span,
                    is_root=span is root,
                    session_id=session_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    capture=capture,
                    turns=turns,
                    transcript=transcript,
                    last_user_text_before=last_user_text_before,
                )
                for span in scope_span.get("spans") or []
            ]
            span_count += len(out_spans)
            trace_ids.update(s["traceId"] for s in out_spans)
            scope_out: Dict[str, Any] = {"spans": out_spans}
            if isinstance(scope_span.get("scope"), Mapping):
                scope = scope_span["scope"]
                scope_out["scope"] = {k: scope[k] for k in ("name", "version") if k in scope}
            out_scopes.append(scope_out)
        out_resource_spans.append({"resource": {"attributes": resource_attributes}, "scopeSpans": out_scopes})

    summary = {
        "conversation_id": session_id,
        "trace_ids": sorted(trace_ids),
        "span_count": span_count,
        "root_found": root is not None,
        "turns": sum(1 for _ in turns),
        "transcript_rows": len(transcript) if capture.everything else 0,
    }
    return {"resourceSpans": out_resource_spans}, summary


def _map_span(
    span: Mapping[str, Any],
    *,
    is_root: bool,
    session_id: str,
    agent_id: Optional[str],
    user_id: Optional[str],
    capture: Capture,
    turns: List[Tuple[int, Dict[str, Any], Dict[str, Any]]],
    transcript: List[Dict[str, Any]],
    last_user_text_before: Mapping[str, str],
) -> Dict[str, Any]:
    name = str(span.get("name") or "")
    attrs_in = attributes_to_dict(span.get("attributes"))
    attributes = _filter_attributes(span.get("attributes"), capture)

    out: Dict[str, Any] = {
        "traceId": str(span["traceId"]).lower(),
        "spanId": str(span["spanId"]).lower(),
        "name": name,
    }
    if _parent(span):
        out["parentSpanId"] = _parent(span).lower()
    for key in ("kind", "startTimeUnixNano", "endTimeUnixNano"):
        if key in span:
            out[key] = span[key]

    status = span.get("status")
    if isinstance(status, Mapping):
        status_out = {k: status[k] for k in ("code",) if k in status}
        # Error text is output: one scrubbed line, never a stack trace or a path.
        message = privacy.error_summary(status.get("message")) if capture.output else None
        message = capture.mask("exception.message", message) if message else None
        if message:
            status_out["message"] = message
        out["status"] = status_out

    events = []
    for event in span.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        event_out = {k: event[k] for k in ("name", "timeUnixNano") if k in event}
        event_attributes = _event_attributes(event.get("attributes"), capture)
        if event_attributes:
            event_out["attributes"] = event_attributes
        events.append(event_out)
    if events:
        out["events"] = events
    links = []
    for link in span.get("links") or []:
        if isinstance(link, Mapping) and "traceId" in link and "spanId" in link:
            links.append({"traceId": link["traceId"], "spanId": link["spanId"]})
    if links:
        out["links"] = links

    _set_attr(attributes, "session.id", session_id)

    def content(key: str, value: Any, side: str) -> None:
        if (capture.input if side == INPUT else capture.output) and value not in (None, ""):
            _set_masked(attributes, capture, key, value)

    if is_root:
        _set_attr(attributes, "fi.span.kind", "CONVERSATION")
        _set_attr(attributes, "gen_ai.system", "elevenlabs")
        if agent_id and ATTR_AGENT_ID not in attrs_in:
            _set_attr(attributes, ATTR_AGENT_ID, agent_id)
        start, end = _nanos(span, "startTimeUnixNano"), _nanos(span, "endTimeUnixNano")
        duration: Optional[float] = None
        if start is not None and end is not None and end > start:
            duration = (end - start) / 1_000_000_000
        else:
            fallback = attrs_in.get(ATTR_TIME_IN_CALL)
            if isinstance(fallback, (int, float)) and not isinstance(fallback, bool) and fallback > 0:
                duration = float(fallback)
        if duration is not None:
            _set_attr(attributes, "call.duration", float(duration))
        if turns:
            _set_attr(attributes, "call.total_turns", len(turns))
        # The caller's id from ElevenLabs is an input. One that carries an email address,
        # a local path or a host name is never written.
        if user_id and privacy.scrub_identity(user_id) == user_id:
            content("user.id", user_id, INPUT)
        if transcript and capture.everything:
            rendered = json.dumps(
                [{k: row[k] for k in ("role", "content", "time") if k in row} for row in transcript],
                ensure_ascii=False,
            )
            _set_masked(attributes, capture, "conversation.transcript", rendered)
            _set_masked(attributes, capture, "fi.conversation.transcript", rendered)
            for index, row in enumerate(transcript):
                _set_attr(attributes, "conversation.transcript.{0}.message.role".format(index), row["role"])
                _set_masked(
                    attributes, capture, "conversation.transcript.{0}.message.content".format(index), row["content"]
                )
        first_user = next((r["content"] for r in transcript if r["role"] == "user"), None)
        last_agent = next((r["content"] for r in reversed(transcript) if r["role"] == "assistant"), None)
        content("input.value", first_user, INPUT)
        content("output.value", last_agent, OUTPUT)
    elif name.startswith(TOOL_SPAN_PREFIX):
        _set_attr(attributes, "fi.span.kind", "TOOL")
        tool_name = name[len(TOOL_SPAN_PREFIX):]
        if tool_name:
            _set_attr(attributes, "tool.name", tool_name)
        params, result = attrs_in.get(ATTR_TOOL_PARAMS), attrs_in.get(ATTR_TOOL_RESULT)
        if params not in (None, ""):
            content("input.value", privacy.conversation_text(_as_text(params)), INPUT)
        if result not in (None, ""):
            content("output.value", privacy.conversation_text(_as_text(result)), OUTPUT)
    elif name == AGENT_TURN_SPAN_NAME:
        model = attrs_in.get(ATTR_PRODUCING_LLM)
        if isinstance(model, str) and model:
            _set_attr(attributes, "fi.span.kind", "LLM")
            _set_attr(attributes, "gen_ai.request.model", model)
            input_tokens, output_tokens = _token_totals(attrs_in)
            if input_tokens is not None:
                _set_attr(attributes, "gen_ai.usage.input_tokens", input_tokens)
            if output_tokens is not None:
                _set_attr(attributes, "gen_ai.usage.output_tokens", output_tokens)
            content("input.value", last_user_text_before.get(str(span.get("spanId"))), INPUT)
        else:
            _set_attr(attributes, "fi.span.kind", "CHAIN")
        text = attrs_in.get(ATTR_AGENT_TEXT)
        if isinstance(text, str):
            content("output.value", privacy.conversation_text(text), OUTPUT)
    elif name == USER_TURN_SPAN_NAME:
        _set_attr(attributes, "fi.span.kind", "CHAIN")
        text = attrs_in.get(ATTR_USER_TEXT)
        if isinstance(text, str):
            content("input.value", privacy.conversation_text(text), INPUT)
    # Any other span name passes through with its (filtered) attributes and no kind.

    out["attributes"] = attributes
    return out


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _event_attributes(attributes: Optional[Iterable[Mapping[str, Any]]], capture: Capture) -> List[Dict[str, Any]]:
    """Span event attributes. The exception type always; its message (one scrubbed line)
    only with output capture on; a stack trace never; anything else like a span attribute."""
    kept: List[Dict[str, Any]] = []
    for attribute in attributes or []:
        if not isinstance(attribute, Mapping) or "key" not in attribute:
            continue
        key = str(attribute["key"])
        value = any_value_to_python(attribute.get("value", {}))
        if key == "exception.type" and isinstance(value, str):
            kept.append({"key": key, "value": {"stringValue": privacy.scrub_identity(value)}})
        elif key == "exception.message":
            message = privacy.error_summary(value) if capture.output else None
            message = capture.mask(key, message) if message else None
            if message:
                kept.append({"key": key, "value": {"stringValue": message}})
        elif key == "exception.escaped" and isinstance(value, bool):
            kept.append({"key": key, "value": {"boolValue": value}})
        else:
            out = _keep_attribute(key, attribute.get("value", {}), capture)
            if out is not None:
                kept.append(out)
    return kept


def _token_totals(attrs: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    input_tokens = output_tokens = None
    for key, raw in attrs.items():
        if not key.startswith(ATTR_LLM_USAGE_PREFIX):
            continue
        usage = _parse_llm_usage(raw)
        if not usage or "tokens" not in usage:
            continue
        kind = key.rsplit(".", 1)[-1]
        tokens = int(usage["tokens"])
        if kind == "input":
            input_tokens = (input_tokens or 0) + tokens
        elif kind in ("output_total", "output"):
            output_tokens = (output_tokens or 0) + tokens
    return input_tokens, output_tokens


def iter_spans(export_request: Mapping[str, Any]) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Yield ``(resource_attributes, span)`` pairs of an export request (tests, checks)."""
    for resource_span in export_request.get("resourceSpans") or []:
        resource = attributes_to_dict((resource_span.get("resource") or {}).get("attributes"))
        for scope_span in resource_span.get("scopeSpans") or []:
            for span in scope_span.get("spans") or []:
                yield resource, span
