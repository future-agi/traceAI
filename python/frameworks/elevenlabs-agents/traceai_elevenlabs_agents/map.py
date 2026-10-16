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
* with content capture on: the transcript (``conversation.transcript``,
  ``fi.conversation.transcript``, flattened rows) and ``input.value`` /
  ``output.value`` on turn and tool spans.

Content capture is off by default. Off means an allowlist: ids, timings, counts,
status codes, model names and token numbers survive; transcript text, tool
arguments and results, reasoning, analysis, dynamic variables, user ids, status
messages and event attributes do not.

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

# Strings that are identifiers or configuration, never conversation content.
_SAFE_STRING_KEYS = frozenset(
    {
        "service.name",
        ATTR_CONVERSATION_ID,
        ATTR_AGENT_ID,
        "elevenlabs.source",
        "elevenlabs.environment",
        "elevenlabs.cost_fiat",
        ATTR_PRODUCING_LLM,
        "elevenlabs.rag.embedding_model",
    }
)
# Numbers under these prefixes can still be content (a collected age, an account
# number in a dynamic variable), so they are dropped when content capture is off.
_CONTENT_PREFIXES = (
    "elevenlabs.dynamic_variable",
    "elevenlabs.analysis",
    "elevenlabs.data_collection",
    "elevenlabs.event.data",
    "elevenlabs.user",
    "elevenlabs.agent.text",
    "elevenlabs.tool.params",
    "elevenlabs.tool.result",
    "elevenlabs.reasoning",
    "elevenlabs.rag.retrieval_query",
)
# Set by this module or by the platform, never accepted from the payload.
_RESERVED_RESOURCE_KEYS = frozenset({"project_name", "project_type", "project_version_name", "project_version_id"})
_RESERVED_RESOURCE_PREFIXES = ("fi.",)

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX16 = re.compile(r"^[0-9a-fA-F]{16}$")
_SIGNED_URL = re.compile(r"^https?://\S+\?\S*$", re.IGNORECASE)


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


def _contains_signed_url(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_SIGNED_URL.match(value.strip()))
    if isinstance(value, list):
        return any(_contains_signed_url(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_signed_url(v) for v in value.values())
    return False


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


def _is_safe_metadata(key: str, value: Any) -> bool:
    """True when an attribute is allowed through with content capture off."""
    if key in _SAFE_STRING_KEYS:
        return isinstance(value, (str, int, float, bool))
    if key.startswith(ATTR_LLM_USAGE_PREFIX):
        return _parse_llm_usage(value) is not None
    if key == ATTR_TIME_IN_CALL:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if key.startswith("elevenlabs.") and not key.startswith(_CONTENT_PREFIXES):
        return isinstance(value, (int, float, bool))
    return False


def _filter_attributes(
    attributes: Optional[Iterable[Mapping[str, Any]]], capture_content: bool
) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for attribute in attributes or []:
        if not isinstance(attribute, Mapping) or "key" not in attribute:
            continue
        key = str(attribute["key"])
        value = any_value_to_python(attribute.get("value", {}))
        if _contains_signed_url(value):
            continue
        if capture_content or _is_safe_metadata(key, value):
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
    capture_content: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return ``(export_request, summary)``.

    Raises :class:`MappingInputError` for a payload that is not a trace.
    Any other exception is a bug in this module and must not be exported.
    """
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
        text = attrs.get(ATTR_USER_TEXT if is_user else ATTR_AGENT_TEXT)
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
            for a in _filter_attributes(resource_in.get("attributes"), capture_content)
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
                    capture_content=capture_content,
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
        "transcript_rows": len(transcript) if capture_content else 0,
    }
    return {"resourceSpans": out_resource_spans}, summary


def _map_span(
    span: Mapping[str, Any],
    *,
    is_root: bool,
    session_id: str,
    agent_id: Optional[str],
    user_id: Optional[str],
    capture_content: bool,
    turns: List[Tuple[int, Dict[str, Any], Dict[str, Any]]],
    transcript: List[Dict[str, Any]],
    last_user_text_before: Mapping[str, str],
) -> Dict[str, Any]:
    name = str(span.get("name") or "")
    attrs_in = attributes_to_dict(span.get("attributes"))
    attributes = _filter_attributes(span.get("attributes"), capture_content)

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
        if capture_content and status.get("message"):
            status_out["message"] = status["message"]
        out["status"] = status_out

    events = []
    for event in span.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        event_out = {k: event[k] for k in ("name", "timeUnixNano") if k in event}
        if capture_content and event.get("attributes"):
            event_out["attributes"] = _filter_attributes(event.get("attributes"), True)
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
        if capture_content:
            if user_id:
                _set_attr(attributes, "user.id", user_id)
            if transcript:
                rendered = json.dumps(
                    [{k: row[k] for k in ("role", "content", "time") if k in row} for row in transcript],
                    ensure_ascii=False,
                )
                _set_attr(attributes, "conversation.transcript", rendered)
                _set_attr(attributes, "fi.conversation.transcript", rendered)
                for index, row in enumerate(transcript):
                    _set_attr(attributes, "conversation.transcript.{0}.message.role".format(index), row["role"])
                    _set_attr(attributes, "conversation.transcript.{0}.message.content".format(index), row["content"])
                first_user = next((r["content"] for r in transcript if r["role"] == "user"), None)
                last_agent = next((r["content"] for r in reversed(transcript) if r["role"] == "assistant"), None)
                if first_user:
                    _set_attr(attributes, "input.value", first_user)
                if last_agent:
                    _set_attr(attributes, "output.value", last_agent)
    elif name.startswith(TOOL_SPAN_PREFIX):
        _set_attr(attributes, "fi.span.kind", "TOOL")
        tool_name = name[len(TOOL_SPAN_PREFIX):]
        if tool_name:
            _set_attr(attributes, "tool.name", tool_name)
        if capture_content:
            params, result = attrs_in.get(ATTR_TOOL_PARAMS), attrs_in.get(ATTR_TOOL_RESULT)
            if params not in (None, ""):
                _set_attr(attributes, "input.value", params if isinstance(params, str) else json.dumps(params))
            if result not in (None, ""):
                _set_attr(attributes, "output.value", result if isinstance(result, str) else json.dumps(result))
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
            if capture_content and last_user_text_before.get(str(span.get("spanId"))):
                _set_attr(attributes, "input.value", last_user_text_before[str(span.get("spanId"))])
        else:
            _set_attr(attributes, "fi.span.kind", "CHAIN")
        text = attrs_in.get(ATTR_AGENT_TEXT)
        if capture_content and isinstance(text, str) and text:
            _set_attr(attributes, "output.value", text)
    elif name == USER_TURN_SPAN_NAME:
        _set_attr(attributes, "fi.span.kind", "CHAIN")
        text = attrs_in.get(ATTR_USER_TEXT)
        if capture_content and isinstance(text, str) and text:
            _set_attr(attributes, "input.value", text)
    # Any other span name passes through with its (filtered) attributes and no kind.

    out["attributes"] = attributes
    return out


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
