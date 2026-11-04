"""Synthetic fixture to verify declared core compatibility and hidden error details."""
import asyncio
import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_openai import OpenAIInstrumentor

MESSAGE = 'synthetic-private-upstream-error-483129'

@pytest.mark.parametrize('async_', [False, True])
def test_hidden_failed_response(async_, monkeypatch):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', 'true')
    exporter = InMemorySpanExporter()
    provider = register(project_name='core-floor-probe', project_type=ProjectType.OBSERVE,
                        span_exporter=exporter, batch=False, verbose=False)
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True))
    body = {'id': 'resp_probe', 'object': 'response', 'created_at': 1, 'model': 'synthetic',
            'status': 'failed', 'output': [], 'error': {'code': 'server_error', 'message': MESSAGE},
            'usage': {'input_tokens': 1, 'output_tokens': 0, 'total_tokens': 1}}
    def handler(req): return httpx.Response(200, json=body)
    def make_client():
        http = (httpx.AsyncClient if async_ else httpx.Client)(transport=httpx.MockTransport(handler), trust_env=False)
        return (openai.AsyncOpenAI if async_ else openai.OpenAI)(api_key='synthetic-key', base_url='https://api.parallel.ai/v1', max_retries=0, http_client=http)
    try:
        if async_:
            async def run():
                async with make_client() as client:
                    assert (await client.responses.create(model='synthetic', input='private input')).status == 'failed'
            asyncio.run(run())
        else:
            with make_client() as client:
                assert client.responses.create(model='synthetic', input='private input').status == 'failed'
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert MESSAGE not in spans[0].to_json()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
