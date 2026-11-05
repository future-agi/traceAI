"""Round 3 independent real SDK probes; synthetic credentials and MockTransport only."""
import asyncio
import json
import httpx
import openai
from typing import Any, Dict
import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor

SECRET_INPUT = 'synthetic-private-system-instruction-827391'

def body(status='completed'):
    return {'id': 'resp_independent', 'object': 'response', 'created_at': 1,
            'model': 'parallel', 'status': status, 'parallel_tool_calls': True,
            'tool_choice': 'auto', 'tools': [], 'instructions': SECRET_INPUT,
            'output': [{'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [{'type': 'output_text',
                        'text': 'safe public answer', 'annotations': []}]}],
            'error': None, 'usage': {'input_tokens': 5, 'output_tokens': 2,
            'total_tokens': 7, 'input_tokens_details': {'cached_tokens': 0},
            'output_tokens_details': {'reasoning_tokens': 0}}}

@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = register(project_name='review3', project_type=ProjectType.OBSERVE,
                        span_exporter=exporter, batch=False, verbose=False)
    OpenAIInstrumentor().instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True))
    try:
        yield provider, exporter
    finally:
        OpenAIInstrumentor().uninstrument()
        provider.force_flush()
        provider.shutdown()

def client(handler, async_=False, base_url='https://api.parallel.ai/v1'):
    http = (httpx.AsyncClient if async_ else httpx.Client)(
        transport=httpx.MockTransport(handler), trust_env=False)
    return (openai.AsyncOpenAI if async_ else openai.OpenAI)(
        api_key='synthetic-api-key-938472', base_url=base_url,
        max_retries=0, http_client=http)

@pytest.mark.parametrize('async_', [False, True])
@pytest.mark.parametrize('to_dict', [False, True])
def test_explicit_dict_parse_keeps_inputs_hidden(tracing, async_, to_dict):
    provider, exporter = tracing
    def handler(request):
        return httpx.Response(200, json=body())
    async def run():
        async with client(handler, True) as c:
            async with c.responses.with_streaming_response.create(
                    model='parallel', instructions=SECRET_INPUT, input='question') as raw:
                result = await raw.parse(**({'to': Dict[str, Any] if openai.__version__.startswith('1.') else dict} if to_dict else {}))
                assert (result['instructions'] if to_dict else result.instructions) == SECRET_INPUT
    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            with c.responses.with_streaming_response.create(
                    model='parallel', instructions=SECRET_INPUT, input='question') as raw:
                result = raw.parse(**({'to': Dict[str, Any] if openai.__version__.startswith('1.') else dict} if to_dict else {}))
                assert (result['instructions'] if to_dict else result.instructions) == SECRET_INPUT
    provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert 'safe public answer' in span.attributes['output.value']
    leaked = {k: v for k, v in span.attributes.items() if SECRET_INPUT in str(v)}
    assert not leaked, leaked

@pytest.mark.parametrize('async_', [False, True])
@pytest.mark.parametrize('method', ['parse', 'json', 'read', 'text', 'iter_lines'])
def test_successful_raw_body_consumption_is_not_error(tracing, async_, method):
    provider, exporter = tracing
    payload = json.dumps(body()).encode()
    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            yield payload
    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
    def handler(request):
        return httpx.Response(200, headers={'content-type':'application/json'},
                              stream=AsyncBody() if async_ else SyncBody())
    async def run():
        async with client(handler, True) as c:
            async with c.responses.with_streaming_response.create(model='parallel', input='question') as raw:
                if method == 'iter_lines':
                    result = [line async for line in raw.iter_lines()]
                else:
                    result = await getattr(raw, method)()
                assert result
    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            with c.responses.with_streaming_response.create(model='parallel', input='question') as raw:
                result = list(raw.iter_lines()) if method == 'iter_lines' else getattr(raw, method)()
                assert result
    provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK, span.to_json()

@pytest.mark.parametrize('async_', [False, True])
@pytest.mark.parametrize('instrumented', [False, True])
def test_raw_malformed_body_remains_inspectable(tracing, async_, instrumented):
    provider, exporter = tracing
    if not instrumented:
        OpenAIInstrumentor().uninstrument()
    def handler(request):
        return httpx.Response(200, headers={'content-type': 'application/json'}, content=b'{"truncated":')
    async def run():
        async with client(handler, True) as c:
            raw = await c.responses.with_raw_response.create(model='parallel', input='question')
            assert raw.status_code == 200
            assert raw.content == b'{"truncated":'
            with pytest.raises(json.JSONDecodeError):
                raw.parse()
    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            raw = c.responses.with_raw_response.create(model='parallel', input='question')
            assert raw.status_code == 200
            assert raw.content == b'{"truncated":'
            with pytest.raises(json.JSONDecodeError):
                raw.parse()

@pytest.mark.parametrize('async_', [False, True])
def test_speech_stream_to_file_not_marked_failed(tracing, async_, tmp_path):
    from pathlib import Path
    provider, exporter = tracing
    payload = b'synthetic-audio-data'
    target = tmp_path / ('audio-async.bin' if async_ else 'audio-sync.bin')
    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            yield payload
    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
    def handler(request):
        return httpx.Response(200, headers={'content-type':'audio/mpeg'},
                              stream=AsyncBody() if async_ else SyncBody())
    async def run():
        async with client(handler, True, base_url='https://api.openai.com/v1') as c:
            async with c.audio.speech.with_streaming_response.create(
                    model='tts-1', voice='alloy', input='Hello') as raw:
                await raw.stream_to_file(target)
    if async_:
        asyncio.run(run())
    else:
        with client(handler, base_url='https://api.openai.com/v1') as c:
            with c.audio.speech.with_streaming_response.create(
                    model='tts-1', voice='alloy', input='Hello') as raw:
                raw.stream_to_file(target)
    assert target.read_bytes() == payload
    provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK, span.to_json()


@pytest.mark.parametrize('async_', [False, True])
def test_early_aborted_raw_iterator_is_error(tracing, async_):
    provider, exporter = tracing
    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/plain'}, content=b'line one\nline two\n')
    async def run():
        async with client(handler, True) as c:
            async with c.responses.with_streaming_response.create(model='parallel', input='question') as raw:
                iterator = raw.iter_lines()
                assert await iterator.__anext__() == 'line one'
                await iterator.aclose()
    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            with c.responses.with_streaming_response.create(model='parallel', input='question') as raw:
                iterator = raw.iter_lines()
                assert next(iterator) == 'line one'
                iterator.close()
    provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
