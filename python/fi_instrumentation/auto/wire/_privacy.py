"""The export-time privacy boundary. All work is on bounded, detached snapshots."""

import json
import re
from itertools import islice
from typing import Any, Iterable, Mapping, Tuple
from urllib.parse import parse_qsl, urlsplit

from fi_instrumentation import _secrets
from fi_instrumentation._attribute_limits import admission_hints, snapshot_attributes, snapshot_sequence
from fi_instrumentation._privacy_catalog import CATALOG, PRIVACY, credential_name, leaf

# Keep the existing S1 snapshot ceiling, within the catalog's maximum budget.
_MAX_NODES = min(4096, CATALOG["limits"]["max_nodes"])
_MAX_TEXT = CATALOG["limits"]["max_text_chars"]


def observe_request(headers: Mapping[Any, Any], url: str = "") -> None:
    """Observe final dispatch data even when dedup/suppression disables capture.

    Never read a body here. Both whole Authorization values and their scheme-free
    credentials are known secrets (in particular, Digest retry Authorization).
    """
    # HTTPX/HTTPX2 .items() comma-joins repeated headers, losing the individual
    # values that actually cross the transport. Prefer their lossless entry API;
    # multidict .items() already preserves duplicates, and ordinary mappings work
    # unchanged. Never split on commas: a credential can itself contain them.
    multi_items = getattr(headers, "multi_items", None)
    entries = multi_items() if callable(multi_items) else headers.items()
    for key, value in entries:
        # Requests/http.client allow bytes headers. HTTP's Latin-1 mapping is
        # lossless, unlike str(bytes), which adds a b'...' repr and escapes bytes.
        # Call builtins directly: multidict.istr and bytes subclasses may have
        # user-defined __str__/decode/lower methods that observation must not run.
        if isinstance(key, bytes):
            key = bytes.decode(key, "latin-1")
        elif isinstance(key, str):
            key = str.__str__(key)
        else:
            continue
        if not credential_name(key):
            continue
        if isinstance(value, bytes):
            value = bytes.decode(value, "latin-1")
        elif isinstance(value, str):
            value = str.__str__(value)
        else:
            continue
        _secrets.register_secret(value)
        if leaf(key).endswith("authorization"):
            parts = value.split(None, 1)
            if len(parts) == 2:
                _secrets.register_secret(parts[1])
    if url:
        for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            if credential_name(key, query=True):
                _secrets.register_secret(value)


def observe_body(value: Any, depth: int = 0, _budget: Any = None, _path: str = "") -> None:
    """Harvest only already-parsed bodies, retaining credential path ancestry."""
    budget = _budget if _budget is not None else [_MAX_NODES]
    budget[0] -= 1
    if depth > CATALOG["limits"]["max_depth"] or budget[0] < 0:
        return
    if type(value) is dict:
        for key, item in islice(dict.items(value), max(0, budget[0])):
            if type(key) is str:
                path = _path + "." + key if _path else key
                observe_body(item, depth + 1, budget, path)
            if budget[0] <= 0:
                break
    elif type(value) in (list, tuple):
        for item in islice(value, max(0, budget[0])):
            observe_body(item, depth + 1, budget, _path)
            if budget[0] <= 0:
                break
    elif type(value) is str and credential_name(_path):
        _secrets.register_secret(value)
        if leaf(_path).endswith("authorization"):
            parts = value.split(None, 1)
            if len(parts) == 2:
                _secrets.register_secret(parts[1])


def secrets(headers: Mapping[str, str], configured: Iterable[str] = (), url: str = "") -> Tuple[str, ...]:
    """Best-effort upstream scrubbing; only export_span is authoritative/fail-closed."""
    try:
        for value in configured:
            _secrets.register_secret(value)
        observe_request(headers, url)
        return _secrets.snapshot()
    except Exception:
        # Do not discard span identity/timing on an upstream failure. The final
        # boundary independently reads the registry and emits a content-free copy.
        return ()


def scrub(value: Any, credentials: Iterable[str] = (), key: str = "", depth: int = 0,
          _budget: Any = None, *, siblings: Any = None, framed: bool = True,
          _hints: Any = None) -> Any:
    """S1 safe walking with the same S6 recognition at every capture/export site."""
    budget = _budget if _budget is not None else [_MAX_NODES, _MAX_TEXT]
    # Export already holds the registry's immutable longest-first snapshot. Sort
    # only standalone/upstream calls, once per walk, never per leaf string.
    if _budget is None:
        credentials = tuple(sorted((v for v in credentials if type(v) is str), key=len, reverse=True))
    hints = PRIVACY.index_hints(siblings.items()) if siblings is not None else _hints
    return PRIVACY.scrub(value, credentials, key, depth, budget, hints, framed)


def mask_attributes(attributes: Any, config: Any, credentials: Iterable[str], operation: str = "",
                    _budget: Any = None, admitted: Any = None) -> dict:
    """``admitted``: sibling hints recorded when the attributes entered the SDK container, so
    media context that admission eviction or A7 projection removed still binds its payload."""
    from fi_instrumentation.instrumentation.config import REDACTED_VALUE

    out = {}
    budget = _budget if _budget is not None else [_MAX_NODES, _MAX_TEXT]
    hints = PRIVACY.index_hints(islice((attributes or {}).items(), _MAX_NODES), hints=admitted)
    for key, value in islice((attributes or {}).items(), _MAX_NODES):
        if budget[0] <= 0:
            break
        if type(key) is not str or len(key) > 1024:
            continue
        input_side = (key.startswith(("input.", "gen_ai.input.", "llm.input_messages",
                                    "gen_ai.system_instructions", "gen_ai.tool.", "gen_ai.prompts",
                                    "llm.prompt_template.variables"))
                      or key in ("raw.input", "gen_ai.request.parameters", "llm.invocation_parameters"))
        input_side = input_side or key in {
            "input", "text", "system_instruction", "tool.arguments", "tools", "tools.definitions",
            "lk.pii.user_input", "lk.user_input", "lk.pii.instructions", "lk.instructions",
            "lk.pii.chat_ctx", "lk.chat_ctx", "lk.pii.input_text", "lk.input_text",
            "lk.pii.function_tool.arguments", "lk.function_tool.arguments", "lk.function_tools",
        }
        output_side = (key.startswith(("output.", "gen_ai.output.", "llm.output_messages",
                                      "gen_ai.tool.call.result")) or key == "raw.output")
        output_side = output_side or key in {
            "output", "transcript", "tool.result", "lk.pii.response.text", "lk.response.text",
            "lk.pii.response.function_calls", "lk.response.function_calls",
            "lk.pii.function_tool.output", "lk.function_tool.output",
            "lk.pii.user_transcript", "lk.user_transcript",
        }
        if config.hide_llm_invocation_parameters:
            if key.startswith("gen_ai.request.") or key == "llm.invocation_parameters":
                continue
            if key in ("input.value", "raw.input"):
                value = REDACTED_VALUE
        if key == "gen_ai.audio.transcript":
            input_side = operation in ("", "text_to_speech")
            output_side = operation != "text_to_speech"
        structural = bool(re.search(
            r"(?:\.message\.(?:role|tool_call_id)|\.tool_call\.(?:id|function\.name))$", key))
        plain_text = (key.endswith((".message.content", ".message_content.text"))
                      or key == "gen_ai.audio.transcript")
        image = ("image" in key.lower() or "file" in key.lower())
        hidden_images = config.hide_input_images and (
            input_side and not structural and not plain_text or image)
        value = scrub(value, credentials, key, _budget=budget, _hints=hints)
        if (input_side and (config.hide_inputs or config.hide_input_messages)
                or output_side and (config.hide_outputs or config.hide_output_messages)
                or input_side and not structural and config.hide_input_text
                or output_side and not structural and config.hide_output_text or hidden_images):
            value = REDACTED_VALUE
        value = config.mask(key, value)
        if value is not None:
            if type(value) is dict:
                value = json.dumps(value, ensure_ascii=False, allow_nan=False)
            elif type(value) is list:
                value = [json.dumps(v, ensure_ascii=False, allow_nan=False) if type(v) in (dict, list)
                         else "NoneType" if v is None else v for v in value]
            out[scrub(key, credentials, _budget=budget)] = value
    return out


def export_span(span: Any, config: Any, configured: Iterable[str] = ()) -> Any:
    """Sanitize every OTLP surface, falling back to identity/timing only on any error."""
    from fi_instrumentation import TraceConfig
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import Link, SpanContext, Status, TraceState

    config = config or TraceConfig()
    try:
        # Legacy direct callers may supply extra values; register them globally.
        for value in configured:
            _secrets.register_secret(value)
        credentials = _secrets.snapshot()
        operation = (span.attributes or {}).get("gen_ai.operation.name", "")
        budget = [_MAX_NODES, _MAX_TEXT]
        attrs = lambda owner: snapshot_attributes(
            mask_attributes(owner.attributes, config, credentials, operation, budget,
                            admitted=admission_hints(getattr(owner, "_attributes", None))),
            getattr(owner, "_attributes", None))
        text = lambda v: config.mask("telemetry.name", scrub(v, credentials, _budget=budget))
        detail = lambda v: config.mask("exception.message", scrub(v, credentials, _budget=budget))

        def context(ctx: Any) -> Any:
            if ctx is None:
                return None
            return SpanContext(ctx.trace_id, ctx.span_id, ctx.is_remote, ctx.trace_flags,
                               TraceState([(scrub(k, credentials, _budget=budget), scrub(v, credentials, k, _budget=budget))
                                           for k, v in ctx.trace_state.items()]))

        scope = span.instrumentation_scope
        if scope is not None:
            copied_scope = InstrumentationScope(text(scope.name) or "", text(scope.version), text(scope.schema_url) or "")
            # Constructors validate homogeneous arrays before OTLP encoding. A safe
            # detached mapping preserves mixed numeric/non-finite arrays element-wise.
            copied_scope._attributes = attrs(scope)
            scope = copied_scope
        resource = span.resource
        copied_resource = Resource({}, schema_url=text(resource.schema_url) or "") if resource else Resource({})
        if resource:
            copied_resource._attributes = attrs(resource)
        clean = ReadableSpan(
            name=text(span.name) or "span", context=context(span.context), parent=context(span.parent),
            resource=copied_resource,
            attributes=attrs(span),
            events=snapshot_sequence(
                [Event(text(e.name) or "event", attrs(e), e.timestamp) for e in islice(span.events or (), 128)],
                getattr(span, "_events", None)),
            links=snapshot_sequence(
                [Link(context(link.context), attrs(link)) for link in islice(span.links or (), 128)],
                getattr(span, "_links", None)),
            kind=span.kind, status=Status(span.status.status_code, detail(span.status.description)),
            start_time=span.start_time, end_time=span.end_time, instrumentation_scope=scope,
        )
        return clean
    except Exception:
        # This path intentionally calls neither the failed masker nor user stringifiers.
        from opentelemetry.trace import SpanContext

        ctx = span.context
        ctx = SpanContext(ctx.trace_id, ctx.span_id, ctx.is_remote, ctx.trace_flags)
        parent = span.parent
        if parent is not None:
            parent = SpanContext(parent.trace_id, parent.span_id, parent.is_remote, parent.trace_flags)
        return ReadableSpan(name="span", context=ctx, parent=parent, resource=Resource({}),
                            attributes=snapshot_attributes({}, getattr(span, "_attributes", None)),
                            events=snapshot_sequence([], getattr(span, "_events", None)),
                            links=snapshot_sequence([], getattr(span, "_links", None)), kind=span.kind,
                            status=Status(span.status.status_code), start_time=span.start_time,
                            end_time=span.end_time)


def protect_live_span(span: Any, config: Any) -> None:
    """Compatibility for standalone FITracer; never rewrite a live SDK span."""
    # Install a snapshotting processor only for this span. Other exporters never see
    # an in-place mutation of attributes, events, resources, or status.
    processor = getattr(span, "_span_processor", None)
    if processor is None or getattr(processor, "_traceai_privacy", False):
        return
    from opentelemetry.sdk.trace import SpanProcessor

    class SnapshotProcessor(SpanProcessor):
        _traceai_privacy = True

        def _on_ending(self, live: Any) -> None:
            callback = getattr(processor, "_on_ending", None)
            if callback is not None:
                callback(live)

        def on_end(self, ended: Any) -> None:
            processor.on_end(export_span(ended, config))

    span._span_processor = SnapshotProcessor()
