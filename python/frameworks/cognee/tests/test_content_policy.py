"""PII and model metadata policies apply to the destination copy, not native spans."""
import json

import pytest
from fi_instrumentation.instrumentation.config import TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cognee._exporter import CogneeExporter, project


@pytest.fixture
def source_span():
    provider = TracerProvider(shutdown_on_exit=False)
    raw = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(raw))

    def emit(attrs):
        with provider.get_tracer('test').start_as_current_span('memory.retrieve', attributes=attrs):
            pass
        return raw.get_finished_spans()[-1]

    yield emit
    provider.shutdown()


@pytest.mark.parametrize('pii', [False, True])
@pytest.mark.parametrize('capture', [False, True])
@pytest.mark.parametrize('hidden', [None, 'input', 'output', 'both'])
def test_all_native_content_uses_shared_pii_policy(monkeypatch, pii, capture, hidden):
    monkeypatch.setenv('FI_PII_REDACTION', str(pii).lower())
    monkeypatch.setenv('FI_HIDE_INPUTS', str(hidden in ('input', 'both')).lower())
    monkeypatch.setenv('FI_HIDE_OUTPUTS', str(hidden in ('output', 'both')).lower())
    inputs = ('langfuse.observation.input', 'memory.query.text', 'cognee.search.query', 'cognee.db.query')
    content = {
        **{key: json.dumps({'question': 'Contact synthetic.input@example.com',
                           'api_key': 'synthetic-private-credential'}) for key in inputs},
        'langfuse.observation.output': json.dumps({'answer': 'Contact synthetic.output@example.com',
                                                'password': 'synthetic-private-credential'}),
    }
    config = TraceConfig()
    raw, sink = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    # Register raw last to catch any mutation made while exporting the FI copy.
    provider.add_span_processor(SimpleSpanProcessor(CogneeExporter(sink, 'policy', capture)))
    provider.add_span_processor(SimpleSpanProcessor(raw))
    try:
        with provider.get_tracer('test').start_as_current_span('memory.retrieve', attributes=content):
            pass
        native, = raw.get_finished_spans()
        projected, = sink.get_finished_spans()
        assert dict(native.attributes) == content
        assert native.context == projected.context
        wire = encode_spans([projected]).SerializeToString()
        assert b'synthetic-private-credential' not in wire
        for direction, keys in (('input', inputs), ('output', ('langfuse.observation.output',))):
            visible = capture and hidden not in (direction, 'both')
            marker = f'synthetic.{direction}@example.com'
            assert (marker.encode() in wire) == (visible and not pii)
            for key in keys:
                assert (key in projected.attributes) == visible
                if visible:
                    expected = json.loads(content[key])
                    expected['api_key' if direction == 'input' else 'password'] = '[REDACTED]'
                    expected = config.mask(key, json.dumps(expected, separators=(',', ':')))
                    assert projected.attributes[key] == expected
                    assert ('<EMAIL_ADDRESS>' in expected) == pii
            assert (direction + '.value' in projected.attributes) == visible
            if visible:
                assert projected.attributes[direction + '.value'] == projected.attributes[keys[0]]
                assert projected.attributes[direction + '.mime_type'] == 'application/json'
    finally:
        provider.shutdown()


@pytest.mark.parametrize('key', ['gen_ai.request.model', 'gen_ai.response.model'])
@pytest.mark.parametrize('capture', [False, True])
@pytest.mark.parametrize('model', [
    'llama3.2:3b', 'library/llama3.2:3b', 'provider/model-name:0',
    'openai/gpt-4o-mini', 'organization/model/variant:v1.2', 'a' * 256,
])
def test_valid_model_identifiers_survive(source_span, key, capture, model):
    native = source_span({key: model})
    projected = project(native, 'models', capture)
    assert projected.attributes[key] == model
    assert dict(native.attributes) == {key: model}
    assert model.encode() in encode_spans([projected]).SerializeToString()


@pytest.mark.parametrize('key', ['gen_ai.request.model', 'gen_ai.response.model'])
@pytest.mark.parametrize('model', [
    'https://host.invalid/secret', 'http://user:synthetic-secret@host.invalid/model',
    '//host.invalid/model', 'model?api_key=synthetic-secret', 'model#synthetic-secret',
    'user:synthetic-secret@host.invalid', 'model:tag/secret', 'model::tag',
    'model:tag\n', 'model synthetic-secret', 'a' * 257, '',
])
def test_model_validation_rejects_urls_and_unbounded_content(source_span, key, model):
    projected = project(source_span({key: model}), 'models', False)
    assert key not in projected.attributes
    assert b'synthetic-secret' not in encode_spans([projected]).SerializeToString()


@pytest.mark.parametrize('key', ['gen_ai.system', 'gen_ai.provider.name', 'gen_ai.operation.name',
                                 'cognee.search.type', 'fi.span.kind'])
def test_model_tag_allowance_does_not_expand_other_metadata(source_span, key):
    projected = project(source_span({key: 'value:tag'}), 'models', False)
    if key == 'fi.span.kind':
        # Reject the malformed native kind, but still infer the retrieval kind.
        assert projected.attributes[key] == 'RETRIEVER'
    else:
        assert key not in projected.attributes
    assert b'value:tag' not in encode_spans([projected]).SerializeToString()
