"""Destination-local projection; never mutate the native span or its provider."""

import json
import math
import re
from typing import TypeGuard

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext, Status, StatusCode

KINDS = {"invoke_agent": "AGENT", "chat": "LLM", "execute_tool": "TOOL"}
TOOL_ERROR = "traceai.agentscope.tool.error"
MAX_CONTENT = 65536
_IDENTIFIERS = frozenset(
    {
        "gen_ai.conversation.id",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.agent.name",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "gen_ai.response.id",
        "agentscope.agent.reply_id",
    }
)
_NUMBERS = frozenset(
    {
        "gen_ai.request.temperature",
        "gen_ai.request.top_p",
        "gen_ai.request.top_k",
        "gen_ai.request.max_tokens",
        "gen_ai.request.presence_penalty",
        "gen_ai.request.frequency_penalty",
        "gen_ai.request.seed",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    }
)
_CONTENT = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
    }
)
# Field-name redaction, not free-text secret detection. Token-count keys are not credentials.
_PRIVATE_KEY = re.compile(
    r"(?:apikey|authorization|password|passwd|credential|clientsecret|secretkey|"
    r"privatekey|accesstoken|refreshtoken|authtoken|bearertoken|idtoken|sessiontoken|"
    r"securitytoken|accesskeyid|secretaccesskey|stacktrace|traceback)"
)


def _identifier(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and all(c.isascii() and (c.isalnum() or c in "._-/:@") for c in value)
    )


def _number(value):
    return (type(value) is int and -(2**63) <= value < 2**63) or (
        type(value) is float and math.isfinite(value)
    )


def _redact(value, depth=0):
    if depth > 16:
        raise ValueError("content nesting limit")
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if _PRIVATE_KEY.search(re.sub(r"[^a-z0-9]", "", key.lower()))
                or key.lower() in {"token", "secret"}
                else _redact(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        # Tool arguments can be JSON serialized inside a message's JSON document.
        try:
            nested = json.loads(value)
        except (ValueError, RecursionError):
            # Do not leak a truncated structured payload that might contain credentials.
            return "[OMITTED]"
        return json.dumps(
            _redact(nested, depth + 1), ensure_ascii=False, allow_nan=False
        )
    return value


def _content(value):
    if not isinstance(value, str) or len(value) > MAX_CONTENT:
        return None
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, (dict, list)):
            # AgentScope 2.0.10 serializes tool objects as opaque repr strings.
            # These cannot be field-redacted safely; never parse/eval a repr.
            return None
        return json.dumps(_redact(decoded), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None


def _context(context):
    if context is None:
        return None
    # Tracestate can carry arbitrary vendor content. IDs and sampling are enough here.
    return SpanContext(
        context.trace_id, context.span_id, context.is_remote, context.trace_flags
    )


def is_native(span: ReadableSpan) -> bool:
    scope = span.instrumentation_scope
    attrs = span.attributes or {}
    operation = attrs.get("gen_ai.operation.name")
    return (
        scope is not None
        and scope.name == "agentscope"
        and isinstance(operation, str)
        and operation in KINDS
        and not attrs.get("agentscope.agent.is_external_execution", False)
    )


def project(
    span: ReadableSpan, project_name: str, capture_content: bool
) -> ReadableSpan | None:
    """Return an allowlisted copy of one real native operation, or None.

    Native external-execution receipt spans are synthetic (no execution timing).
    They and unrelated instrumentation never go to this destination.
    """
    if not is_native(span):
        return None
    attrs = span.attributes or {}
    operation = attrs["gen_ai.operation.name"]
    assert isinstance(operation, str)  # validated by is_native
    out = {
        key: value
        for key, value in attrs.items()
        if (key in _IDENTIFIERS and _identifier(value))
        or (key in _NUMBERS and _number(value))
    }
    out["gen_ai.operation.name"] = operation
    out["fi.span.kind"] = KINDS[operation]
    if "gen_ai.conversation.id" in out:
        out["session.id"] = out["gen_ai.conversation.id"]
    if operation != "chat":
        # Usage belongs only to the native model call, never aggregate it onto parents.
        out.pop("gen_ai.usage.input_tokens", None)
        out.pop("gen_ai.usage.output_tokens", None)

    code = span.status.status_code
    if operation == "execute_tool" and attrs.get(TOOL_ERROR) is True:
        code = StatusCode.ERROR
        out[TOOL_ERROR] = True

    if capture_content:
        for key in _CONTENT:
            value = _content(attrs.get(key))
            if value is not None:
                out[key] = value
        for direction, source in (
            (
                "input",
                "gen_ai.tool.call.arguments"
                if operation == "execute_tool"
                else "gen_ai.input.messages",
            ),
            (
                "output",
                "gen_ai.tool.call.result"
                if operation == "execute_tool"
                else "gen_ai.output.messages",
            ),
        ):
            if source in out:
                out[direction + ".value"] = out[source]
                out[direction + ".mime_type"] = "application/json"

    # Names, resources, scope attributes, status descriptions, events and links are all
    # potential content channels. Reconstruct them instead of proxying the original.
    name_key = {
        "invoke_agent": "gen_ai.agent.name",
        "chat": "gen_ai.request.model",
        "execute_tool": "gen_ai.tool.name",
    }[operation]
    identifier = out.get(name_key)
    name = operation + (" " + identifier if isinstance(identifier, str) else "")
    events = []
    for event in span.events:
        if event.name == "exception":
            exception_type = (event.attributes or {}).get("exception.type")
            safe = (
                {"exception.type": exception_type}
                if _identifier(exception_type)
                else {}
            )
            events.append(Event("exception", safe, event.timestamp))
    return ReadableSpan(
        name=name,
        context=_context(span.context),
        parent=_context(span.parent),
        resource=Resource(
            {
                "project_name": project_name,
                "project_type": "observe",
                "service.name": "agentscope",
            }
        ),
        attributes=out,
        events=events,
        links=[Link(_context(link.context), {}) for link in span.links],
        kind=span.kind,
        status=Status(code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=InstrumentationScope("agentscope"),
    )
