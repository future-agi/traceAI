"""Embedding metadata survives the destination-local privacy projection."""
import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cognee._exporter import project


def native_span(extra=None):
    provider = TracerProvider(shutdown_on_exit=False)
    sink = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(sink))
    attrs = {
        'gen_ai.operation.name': 'embeddings',
        'gen_ai.provider.name': 'openai',
        'gen_ai.request.model': 'text-embedding-3-small',
        'gen_ai.embeddings.dimension.count': 1536,
        'server.address': 'private-model-host.invalid',
        'custom.secret': 'synthetic-private-value',
        **(extra or {}),
    }
    try:
        with provider.get_tracer('native').start_as_current_span('cognee.observe.embed_text', attributes=attrs):
            pass
        return sink.get_finished_spans()[0]
    finally:
        provider.shutdown()


@pytest.mark.parametrize('capture_content', [False, True])
def test_embedding_metadata_kind_and_identity(capture_content):
    source = native_span()
    before = dict(source.attributes)
    projected = project(source, 'embedding-test', capture_content)
    assert projected.attributes['gen_ai.provider.name'] == 'openai'
    assert projected.attributes['gen_ai.embeddings.dimension.count'] == 1536
    assert projected.attributes['gen_ai.operation.name'] == 'embeddings'
    assert projected.attributes['gen_ai.request.model'] == 'text-embedding-3-small'
    assert projected.attributes['fi.span.kind'] == 'EMBEDDING'
    assert projected.context == source.context
    assert projected.parent == source.parent
    assert dict(source.attributes) == before
    assert 'fi.span.kind' not in source.attributes
    assert 'server.address' not in projected.attributes
    assert 'custom.secret' not in projected.attributes
    payload = encode_spans([projected]).SerializeToString()
    assert b'synthetic-private-value' not in payload
    assert b'private-model-host.invalid' not in payload


@pytest.mark.parametrize('key', ['fi.span.kind', 'gen_ai.span.kind', 'llm.request.type', 'openinference.span.kind'])
def test_embedding_inference_preserves_explicit_kind(key):
    projected = project(native_span({key: 'CUSTOM'}), 'embedding-test', False)
    assert projected.attributes[key] == 'CUSTOM'
    if key != 'fi.span.kind':
        assert 'fi.span.kind' not in projected.attributes


def test_new_metadata_uses_existing_wire_and_privacy_guards():
    projected = project(native_span({
        'gen_ai.provider.name': 'https://host.invalid/?api_key=synthetic-secret',
        'gen_ai.embeddings.dimension.count': 2**100,
    }), 'embedding-test', False)
    assert 'gen_ai.provider.name' not in projected.attributes
    assert 'gen_ai.embeddings.dimension.count' not in projected.attributes
    assert b'synthetic-secret' not in encode_spans([projected]).SerializeToString()
