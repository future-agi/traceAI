"""Native SDK stream ownership regressions, using local mock HTTP only."""
import asyncio
import gc
import json
from importlib.metadata import version

import anthropic
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_anthropic import AnthropicInstrumentor

try:
    import httpx2 as httpx
except ImportError:
    import httpx

START = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'content': [], 'model': 'local', 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 4, 'output_tokens': 0}}
EVENTS = [{'type': 'message_start', 'message': START}, {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': '', 'citations': []}}, {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'hello'}}, {'type': 'content_block_stop', 'index': 0}, {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 2}}, {'type': 'message_stop'}]
CITATION = {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'citations_delta', 'citation': {'type': 'char_location', 'cited_text': 'fact', 'document_index': 0, 'document_title': 'doc', 'start_char_index': 0, 'end_char_index': 4}}}
PARAMS = dict(model='local', max_tokens=16, messages=[{'role': 'user', 'content': 'hi'}], stream=True)
SECRET = 'r3_hidden_error_sentinel'


def body(events):
    return ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events)


def make_client(events, asynchronous=False):
    handler = lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=body(events))
    if asynchronous:
        return anthropic.AsyncAnthropic(api_key='local', http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return anthropic.Anthropic(api_key='local', http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def setup():
    p = TracerProvider(); exporter = InMemorySpanExporter(); p.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = AnthropicInstrumentor(); instrumentor.instrument(tracer_provider=p)
    return p, exporter, instrumentor


def assert_span(exporter, error=False, full=True):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == (StatusCode.ERROR if error else StatusCode.OK)
    if full:
        attrs = dict(span.attributes)
        assert 'hello' in str(attrs)
        assert attrs['gen_ai.usage.input_tokens'] == 4
        assert attrs['gen_ai.usage.output_tokens'] == 2
    assert SECRET not in str(span.status) + str(span.attributes) + str([(e.name, dict(e.attributes)) for e in span.events])


@pytest.mark.parametrize('mode', ['temporary', 'break', 'iterator_close', 'stream_close'])
def test_sync_stream_lifetime(mode):
    c = make_client(EVENTS); p, x, i = setup()
    try:
        stream = c.messages.create(**PARAMS)
        if mode == 'temporary':
            assert next(iter(stream)).type == 'message_start'; gc.collect()
        elif mode == 'break':
            for event in stream:
                assert event.type == 'message_start'; break
            gc.collect()
        else:
            it = iter(stream); assert next(it).type == 'message_start'; it.close()
        assert len(x.get_finished_spans()) == 0
        if mode == 'stream_close':
            stream.close(); stream.close(); assert_span(x, full=False)
        else:
            assert list(stream)[-1].type == 'message_stop'; assert_span(x)
            stream.close(); assert_span(x)
    finally:
        i.uninstrument(); c.close(); p.shutdown()


@pytest.mark.parametrize('mode', ['break', 'iterator_close', 'stream_close'])
def test_async_stream_lifetime(mode):
    async def run():
        c = make_client(EVENTS, True); p, x, i = setup()
        try:
            stream = await c.messages.create(**PARAMS)
            if mode == 'break':
                async for event in stream:
                    assert event.type == 'message_start'; break
                await asyncio.sleep(0); gc.collect(); await asyncio.sleep(0)
            else:
                it = stream.__aiter__(); assert (await it.__anext__()).type == 'message_start'; await it.aclose()
            assert len(x.get_finished_spans()) == 0
            if mode == 'stream_close':
                await stream.close(); await stream.close(); assert_span(x, full=False)
            else:
                assert [e async for e in stream][-1].type == 'message_stop'; assert_span(x)
                await stream.close(); assert_span(x)
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('asynchronous', [False, True])
def test_resumed_api_error_preserves_partial_output_and_privacy(asynchronous, monkeypatch, caplog):
    monkeypatch.setenv('FI_HIDE_ERROR_DETAILS', 'true')
    events = EVENTS[:-1] + [{'type': 'error', 'error': {'type': 'api_error', 'message': SECRET}}]
    async def run_async():
        c = make_client(events, True); p, x, i = setup()
        try:
            stream = await c.messages.create(**PARAMS)
            it = stream.__aiter__(); await it.__anext__(); await it.aclose()
            assert not x.get_finished_spans()
            with pytest.raises(anthropic.APIStatusError, match=SECRET):
                async for _ in stream: pass
            assert_span(x, error=True); assert SECRET not in caplog.text
            await stream.close(); assert_span(x, error=True)
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    if asynchronous:
        asyncio.run(run_async()); return
    c = make_client(events); p, x, i = setup()
    try:
        stream = c.messages.create(**PARAMS); assert next(iter(stream)).type == 'message_start'
        assert not x.get_finished_spans()
        with pytest.raises(anthropic.APIStatusError, match=SECRET): list(stream)
        assert_span(x, error=True); assert SECRET not in caplog.text
        stream.close(); assert_span(x, error=True)
    finally:
        i.uninstrument(); c.close(); p.shutdown()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_native_citation_parity(asynchronous):
    if version('anthropic') == '0.41.0':
        pytest.skip('NA: 0.41.0 has no citations delta in its SDK type contract')
    events = EVENTS[:3] + [CITATION] + EVENTS[3:]
    async def run_async():
        c = make_client(events, True)
        expected = [e.model_dump() async for e in await c.messages.create(**PARAMS)]
        p, x, i = setup()
        try:
            actual = [e.model_dump() async for e in await c.messages.create(**PARAMS)]
            assert actual == expected; assert_span(x)
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    if asynchronous:
        asyncio.run(run_async()); return
    c = make_client(events); expected = [e.model_dump() for e in c.messages.create(**PARAMS)]
    p, x, i = setup()
    try:
        assert [e.model_dump() for e in c.messages.create(**PARAMS)] == expected; assert_span(x)
    finally:
        i.uninstrument(); c.close(); p.shutdown()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_native_thinking_tool_and_unknown_delta_parity(asynchronous):
    if version('anthropic') == '0.41.0':
        pytest.skip('NA: thinking/unknown delta fixture is for current SDK')
    extra = [
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'thinking_delta', 'thinking': 'reason'}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'signature_delta', 'signature': 'signed'}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 'tool1', 'name': 'lookup', 'input': {}}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"x":1}'}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'future_nontext_delta', 'value': 'future'}},
        {'type': 'content_block_stop', 'index': 2},
    ]
    events = EVENTS[:4] + extra + EVENTS[4:]
    # Compare native models, not model_dump(): SDK 0.57.1 accepts unknown
    # deltas but its Pydantic serializer cannot dump their fallback model.
    async def run_async():
        c = make_client(events, True)
        expected = [e async for e in await c.messages.create(**PARAMS)]
        p, x, i = setup()
        try:
            assert [e async for e in await c.messages.create(**PARAMS)] == expected
            assert_span(x)
            assert 'lookup' in str(x.get_finished_spans()[0].attributes)
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    if asynchronous:
        asyncio.run(run_async()); return
    c = make_client(events)
    expected = list(c.messages.create(**PARAMS))
    p, x, i = setup()
    try:
        assert list(c.messages.create(**PARAMS)) == expected
        assert_span(x)
        assert 'lookup' in str(x.get_finished_spans()[0].attributes)
    finally:
        i.uninstrument(); c.close(); p.shutdown()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_resumed_stream_content_privacy_and_usage(asynchronous, monkeypatch):
    monkeypatch.setenv('FI_HIDE_INPUTS', 'true')
    monkeypatch.setenv('FI_HIDE_OUTPUTS', 'true')
    params = dict(PARAMS, messages=[{'role': 'user', 'content': 'r3_private_prompt_sentinel'}])
    def check(x):
        spans = x.get_finished_spans(); assert len(spans) == 1
        span = spans[0]; attrs = dict(span.attributes)
        assert span.status.status_code == StatusCode.OK
        assert 'r3_private_prompt_sentinel' not in str(attrs)
        assert 'hello' not in str(attrs)
        assert attrs['gen_ai.usage.input_tokens'] == 4
        assert attrs['gen_ai.usage.output_tokens'] == 2
    async def run_async():
        c = make_client(EVENTS, True); p, x, i = setup()
        try:
            s = await c.messages.create(**params); it = s.__aiter__(); await it.__anext__(); await it.aclose()
            assert not x.get_finished_spans()
            assert [event async for event in s][-1].type == 'message_stop'; check(x)
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    if asynchronous:
        asyncio.run(run_async()); return
    c = make_client(EVENTS); p, x, i = setup()
    try:
        s = c.messages.create(**params); next(iter(s)); assert not x.get_finished_spans()
        assert list(s)[-1].type == 'message_stop'; check(x)
    finally:
        i.uninstrument(); c.close(); p.shutdown()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_consumer_injected_exception_does_not_finish_stream(asynchronous):
    class ConsumerError(Exception):
        pass
    async def run_async():
        c = make_client(EVENTS, True); p, x, i = setup()
        try:
            s = await c.messages.create(**PARAMS); it = s.__aiter__(); await it.__anext__()
            with pytest.raises(ConsumerError): await it.athrow(ConsumerError('consumer'))
            assert not x.get_finished_spans()
            assert [event async for event in s][-1].type == 'message_stop'; assert_span(x)
        finally:
            i.uninstrument(); await c.close(); p.shutdown()
    if asynchronous:
        asyncio.run(run_async()); return
    c = make_client(EVENTS); p, x, i = setup()
    try:
        s = c.messages.create(**PARAMS); it = iter(s); next(it)
        with pytest.raises(ConsumerError): it.throw(ConsumerError('consumer'))
        assert not x.get_finished_spans()
        assert list(s)[-1].type == 'message_stop'; assert_span(x)
    finally:
        i.uninstrument(); c.close(); p.shutdown()
