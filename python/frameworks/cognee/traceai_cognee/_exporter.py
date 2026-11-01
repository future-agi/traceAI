"""Copy and project native spans at the exporter boundary."""
import json
import math
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Event
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import Status, Link
from ._structured import redact_structured

# Explicit keys: unknown native attributes may contain user content or credentials.
_SAFE = frozenset({'gen_ai.operation.name', 'gen_ai.system', 'gen_ai.request.model',
                   'gen_ai.response.model', 'gen_ai.usage.input_tokens',
                   'gen_ai.usage.output_tokens', 'gen_ai.usage.total_tokens',
                   'llm.token_count.prompt', 'llm.token_count.completion',
                   'llm.token_count.total', 'cognee.search.type'})
_CONTENT = ('langfuse.observation.input', 'langfuse.observation.output',
            'memory.query.text', 'cognee.search.query', 'cognee.db.query')
_KIND_KEYS = ('fi.span.kind', 'gen_ai.span.kind', 'llm.request.type', 'openinference.span.kind')

def _wire_number(value):
    if type(value) is bool:
        return True
    if type(value) is int:
        return -(2**63) <= value < 2**63
    return type(value) is float and math.isfinite(value)


def _safe_value(value):
    return _wire_number(value) or (isinstance(value, str) and len(value) <= 256 and all(c.isalnum() or c in '._-/' for c in value))

def _kind(name):
    if name == 'memory.retrieve' or name.startswith('cognee.search'):
        return 'RETRIEVER'
    if name == 'cognee.observe.acreate_structured_output':
        return 'LLM'
    return None

def _content(value):
    if not isinstance(value, str) and not _wire_number(value):
        return None
    return redact_structured(value)

def project(span, project_name, capture_content):
    attrs = dict(span.attributes or {})
    out = {k: v for k, v in attrs.items() if k in _SAFE and _safe_value(v)}
    for key in _KIND_KEYS:
        if key in attrs and _safe_value(attrs[key]):
            out[key] = attrs[key]
    if not any(k in out for k in _KIND_KEYS) and _kind(span.name):
        out['fi.span.kind'] = _kind(span.name)
    if capture_content:
        for key in _CONTENT:
            value = _content(attrs.get(key))
            if value is not None:
                out[key] = value
        for dest, sources in (('input', ('langfuse.observation.input', 'memory.query.text', 'cognee.search.query')),
                              ('output', ('langfuse.observation.output',))):
            for key in sources:
                if key in out:
                    value = out[key]
                    out[dest + '.value'] = value
                    try:
                        mime = 'application/json' if isinstance(json.loads(value), (dict, list)) else 'text/plain'
                    except (TypeError, ValueError):
                        mime = 'text/plain'
                    out[dest + '.mime_type'] = mime
                    break
    # Even content opt-in never exports error text, stacktraces or arbitrary events.
    events = [Event('exception', {}, e.timestamp) for e in span.events if e.name == 'exception']
    links = [Link(link.context, {}) for link in span.links]
    resource = Resource({'project_name': project_name, 'project_type': 'observe'})
    return ReadableSpan(name=span.name, context=span.context, parent=span.parent,
                        resource=resource, attributes=out, events=events, links=links,
                        kind=span.kind, status=Status(span.status.status_code),
                        start_time=span.start_time, end_time=span.end_time,
                        instrumentation_scope=None)

class CogneeExporter(SpanExporter):
    def __init__(self, exporter, project_name, capture_content=False):
        self.exporter = exporter
        self.project_name = project_name
        self.capture_content = capture_content

    def export(self, spans):
        # Fail closed: a malformed span is dropped rather than exporting native data.
        projected = []
        for span in spans:
            try:
                projected.append(project(span, self.project_name, self.capture_content))
            except Exception:
                continue
        return self.exporter.export(projected)

    def force_flush(self, timeout_millis=30000):
        return self.exporter.force_flush(timeout_millis)

    def shutdown(self):
        self.exporter.shutdown()
