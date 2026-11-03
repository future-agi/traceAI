"""Real Cognee decorators -> adapter -> loopback OTLP, with no model calls.

The separate process owns Cognee/OTel globals and a temporary application home.
Install cognee[tracing] to run this contract (required by cognee-tests.yml).
"""
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile

import pytest


PROGRAM = r'''
import asyncio
import json
import sys
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_cognee import configure

# Reuse the repository's loopback receiver instead of implementing a collector.
sys.path.insert(0, sys.argv[1])
from harness import Receiver, _flatten_attributes

with Receiver() as receiver:
    handle = configure(project_name='cognee-contract', endpoint=receiver.collector_endpoint,
                       headers={'X-Api-Key': 'synthetic-api', 'X-Secret-Key': 'synthetic-secret'})
    raw = InMemorySpanExporter()
    handle.provider.add_span_processor(SimpleSpanProcessor(raw))
    try:
        from cognee.modules.observability.get_observe import get_observe
        observe = get_observe()

        class Engine:
            provider = 'OpenAI'
            model = 'text-embedding-3-small'
            dimensions = 3
            endpoint = 'https://private-model-host.invalid/v1'

            @observe(as_type='embeddings')
            def embed_text(self, text):
                return [0.1, 0.2, 0.3]

            @observe(as_type='embeddings')
            async def embed_documents(self, texts):
                return [[0.1, 0.2, 0.3] for _ in texts]

            @observe(as_type='embeddings')
            async def fail(self, texts):
                raise ValueError('synthetic-private-error')

        engine = Engine()
        with handle.provider.get_tracer('test').start_as_current_span('parent') as parent:
            assert engine.embed_text('synthetic-private-content') == [0.1, 0.2, 0.3]
            assert asyncio.run(engine.embed_documents(['synthetic-private-content'])) == [[0.1, 0.2, 0.3]]
            try:
                asyncio.run(engine.fail(['synthetic-private-content']))
            except ValueError:
                pass
            else:
                raise AssertionError('native error was swallowed')
        assert handle.force_flush()
        exported = receiver.spans()
        source = raw.get_finished_spans()
        embeddings = [s for s in exported if s['name'].startswith('cognee.observe.')]
        native = [s for s in source if s.name.startswith('cognee.observe.')]
        assert len(embeddings) == len(native) == 3
        assert len({s['spanId'] for s in exported}) == len(exported)
        from base64 import b64decode
        # Protobuf JSON represents IDs as base64, not the usual hex strings.
        def span_id(value):
            return int.from_bytes(b64decode(value), 'big')
        for span in embeddings:
            attrs = _flatten_attributes(span['attributes'])
            assert attrs['gen_ai.provider.name'] == 'openai', attrs
            assert int(attrs['gen_ai.embeddings.dimension.count']) == 3, attrs
            assert attrs['gen_ai.operation.name'] == 'embeddings'
            assert attrs['gen_ai.request.model'] == engine.model
            assert attrs['fi.span.kind'] == 'EMBEDDING'
            assert span_id(span['parentSpanId']) == parent.get_span_context().span_id
            assert span_id(span['spanId']) in {s.context.span_id for s in native}
            assert int.from_bytes(b64decode(span['traceId']), 'big') == parent.get_span_context().trace_id
        failed = next(s for s in embeddings if s['name'] == 'cognee.observe.fail')
        assert failed['status']['code'] == 'STATUS_CODE_ERROR'
        assert not failed['status'].get('message')
        wire = json.dumps(exported)
        for private in ('synthetic-private-content', 'synthetic-private-error', 'private-model-host.invalid'):
            assert private not in wire
        assert all('fi.span.kind' not in s.attributes for s in native)
        assert all(s.attributes['server.address'] == 'private-model-host.invalid' for s in native)
        for request in receiver.requests():
            assert request['path'] == '/tracer/v1/traces'
            assert request['headers']['x-api-key'] == 'synthetic-api'
            assert request['headers']['x-secret-key'] == 'synthetic-secret'
            assert all(r['project_name'] == 'cognee-contract' and r['project_type'] == 'observe'
                       for r in request['resource_attributes'])
    finally:
        handle.shutdown()
        handle.provider.shutdown()
'''


@pytest.mark.skipif(importlib.util.find_spec('cognee') is None, reason='install cognee[tracing] for the native contract')
def test_native_embedding_decorators_export_once_with_metadata():
    harness_parent = Path(__file__).resolve().parents[3] / 'tests'
    with tempfile.TemporaryDirectory() as directory:
        env = {
            'PATH': os.environ.get('PATH', ''),
            'HOME': directory,
            'COGNEE_TRACING_ENABLED': 'true',
            'TELEMETRY_DISABLED': 'true',
            'MONITORING_TOOL': 'none',
            'LITELLM_LOCAL_MODEL_COST_MAP': 'True',
        }
        result = subprocess.run(
            [sys.executable, '-I', '-c', PROGRAM, str(harness_parent)],
            env=env, cwd=directory, capture_output=True, text=True, timeout=120,
        )
    assert result.returncode == 0, result.stdout + result.stderr
