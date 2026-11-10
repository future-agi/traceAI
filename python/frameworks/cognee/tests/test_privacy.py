"""Global directional privacy overrides content opt-in at the export boundary."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cognee._exporter import CogneeExporter


@pytest.mark.parametrize('capture_content', [False, True])
@pytest.mark.parametrize('hide_inputs', [None, False, True])
@pytest.mark.parametrize('hide_outputs', [None, False, True])
def test_directional_privacy(monkeypatch, capture_content, hide_inputs, hide_outputs):
    for key, hidden in (('FI_HIDE_INPUTS', hide_inputs), ('FI_HIDE_OUTPUTS', hide_outputs)):
        if hidden is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(hidden).lower())
    inputs = {
        'langfuse.observation.input': 'synthetic-private-prompt',
        'memory.query.text': 'synthetic-private-memory',
        'cognee.search.query': 'synthetic-private-search',
        'cognee.db.query': 'synthetic-private-database',
    }
    outputs = {'langfuse.observation.output': 'synthetic-private-response'}
    attrs = {**inputs, **outputs, 'gen_ai.request.model': 'synthetic-model'}
    raw = InMemorySpanExporter()
    sink = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(raw))
    provider.add_span_processor(SimpleSpanProcessor(CogneeExporter(sink, 'privacy-test', capture_content)))
    try:
        with provider.get_tracer('test').start_as_current_span('memory.retrieve', attributes=attrs):
            pass
        source, = raw.get_finished_spans()
        projected, = sink.get_finished_spans()
        assert dict(source.attributes) == attrs
        assert projected.context == source.context
        assert projected.attributes['gen_ai.request.model'] == 'synthetic-model'
        assert projected.attributes['fi.span.kind'] == 'RETRIEVER'
        wire = encode_spans([projected]).SerializeToString()
        for direction, content, hidden in (('input', inputs, hide_inputs), ('output', outputs, hide_outputs)):
            visible = capture_content and not hidden
            for key, value in content.items():
                assert (key in projected.attributes) == visible
                assert (value.encode() in wire) == visible
            assert (direction + '.value' in projected.attributes) == visible
            assert (direction + '.mime_type' in projected.attributes) == visible
            if visible:
                assert projected.attributes[direction + '.value'] == next(iter(content.values()))
                assert projected.attributes[direction + '.mime_type'] == 'text/plain'
    finally:
        provider.shutdown()


PROGRAM = r'''
import asyncio
import json
import os
import sys
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_cognee import configure
sys.path.insert(0, sys.argv[1])
from harness import Receiver, _flatten_attributes

with Receiver() as receiver:
    handle = configure(project_name='privacy-contract', capture_content=True,
                       endpoint=receiver.collector_endpoint,
                       headers={'X-Api-Key': 'synthetic-api', 'X-Secret-Key': 'synthetic-secret'})
    raw = InMemorySpanExporter()
    handle.provider.add_span_processor(SimpleSpanProcessor(raw))
    try:
        from cognee.modules.observability.get_observe import get_observe
        observe = get_observe()

        class Engine:
            name = 'openai'
            model = 'synthetic-model'

            @observe(as_type='generation')
            async def acreate_structured_output(self, prompt):
                return 'SYNTHETIC-PRIVATE-RESPONSE'

        assert asyncio.run(Engine().acreate_structured_output('SYNTHETIC-PRIVATE-PROMPT')) == 'SYNTHETIC-PRIVATE-RESPONSE'
        assert handle.force_flush()
        span, = receiver.spans()
        native, = raw.get_finished_spans()
        attrs = _flatten_attributes(span['attributes'])
        assert attrs['fi.span.kind'] == 'LLM'
        assert attrs['gen_ai.request.model'] == 'synthetic-model'
        wire = json.dumps(span)
        for direction, marker in (('input', 'SYNTHETIC-PRIVATE-PROMPT'), ('output', 'SYNTHETIC-PRIVATE-RESPONSE')):
            key = 'langfuse.observation.' + direction
            assert marker in native.attributes[key], native.attributes
            visible = os.environ['FI_HIDE_' + direction.upper() + 'S'] == 'false'
            assert (key in attrs) == visible, attrs
            assert (direction + '.value' in attrs) == visible, attrs
            assert (direction + '.mime_type' in attrs) == visible, attrs
            assert (marker in wire) == visible, attrs
    finally:
        handle.shutdown()
        handle.provider.shutdown()
'''


@pytest.mark.skipif(importlib.util.find_spec('cognee') is None, reason='install cognee[tracing] for the native contract')
@pytest.mark.parametrize('hide_inputs,hide_outputs', [(False, False), (True, False), (False, True), (True, True)])
def test_native_generation_privacy(hide_inputs, hide_outputs):
    harness_parent = Path(__file__).resolve().parents[3] / 'tests'
    with tempfile.TemporaryDirectory() as directory:
        env = {
            'PATH': os.environ.get('PATH', ''),
            'HOME': directory,
            'COGNEE_TRACING_ENABLED': 'true',
            'TELEMETRY_DISABLED': 'true',
            'MONITORING_TOOL': 'none',
            'LITELLM_LOCAL_MODEL_COST_MAP': 'True',
            'FI_HIDE_INPUTS': str(hide_inputs).lower(),
            'FI_HIDE_OUTPUTS': str(hide_outputs).lower(),
        }
        result = subprocess.run(
            [sys.executable, '-I', '-c', PROGRAM, str(harness_parent)],
            env=env, cwd=directory, capture_output=True, text=True, timeout=120,
        )
    assert result.returncode == 0, result.stdout + result.stderr
