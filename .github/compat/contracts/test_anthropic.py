"""Anthropic SDK execution over MockTransport, not mocked Messages.create."""
import asyncio
import json

import anthropic
import httpx
import pytest
from opentelemetry.trace import StatusCode
from fi_instrumentation import TraceConfig
from traceai_anthropic import AnthropicInstrumentor

MODEL = 'claude-contract-model'
SECRET = 'private-contract-input'
ANSWER = 'offline answer'


def message():
    return {'id': 'msg_offline', 'type': 'message', 'role': 'assistant', 'model': MODEL,
            'content': [{'type': 'text', 'text': ANSWER}], 'stop_reason': 'end_turn',
            'stop_sequence': None, 'usage': {'input_tokens': 4, 'output_tokens': 2}}


@pytest.fixture
def instrument(tracing):
    provider, exporter = tracing
    instrumentor = AnthropicInstrumentor()
    # Older releases don't restore count_tokens in uninstrument(); isolate this test fixture.
    from anthropic.resources.messages import Messages
    original_count_tokens = Messages.count_tokens

    def start(config=None):
        instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
        assert instrumentor.is_instrumented_by_opentelemetry
        return provider, exporter

    yield start
    instrumentor.uninstrument()
    Messages.count_tokens = original_count_tokens


def sdk(handler, asynchronous=False):
    transport = httpx.MockTransport(handler)
    cls = anthropic.AsyncAnthropic if asynchronous else anthropic.Anthropic
    http_cls = httpx.AsyncClient if asynchronous else httpx.Client
    return cls(api_key='offline-not-a-key', base_url='http://127.0.0.1:9', max_retries=0,
               http_client=http_cls(transport=transport, trust_env=False))


def only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [s.name for s in spans]
    assert spans[0].end_time is not None
    return spans[0]


def arguments():
    return dict(model=MODEL, max_tokens=16, messages=[{'role': 'user', 'content': SECRET}])


def test_sync_parentage_model_and_usage(instrument):
    provider, exporter = instrument()
    with provider.get_tracer('contract').start_as_current_span('parent') as parent:
        with sdk(lambda _: httpx.Response(200, json=message())) as client:
            result = client.messages.create(**arguments())
        assert result.content[0].text == ANSWER
        span = only_span(exporter)
        assert span.parent.span_id == parent.get_span_context().span_id
    assert span.status.status_code == StatusCode.OK
    assert span.attributes['gen_ai.request.model'] == MODEL
    assert span.attributes['gen_ai.usage.input_tokens'] == 4
    assert span.attributes['gen_ai.usage.output_tokens'] == 2


def test_async_sdk_return_value(instrument):
    _, exporter = instrument()
    async def run():
        async with sdk(lambda _: httpx.Response(200, json=message()), True) as client:
            result = await client.messages.create(**arguments())
            assert result.content[0].text == ANSWER
    asyncio.run(run())
    assert only_span(exporter).status.status_code == StatusCode.OK


def test_streaming_sdk_and_span_completion(instrument):
    _, exporter = instrument()
    events = [
        {'type': 'message_start', 'message': {**message(), 'content': [], 'stop_reason': None}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': ANSWER}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 2}},
        {'type': 'message_stop'},
    ]
    body = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events)
    with sdk(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)) as client:
        events = list(client.messages.create(stream=True, **arguments()))
    assert any(getattr(getattr(e, 'delta', None), 'text', None) == ANSWER for e in events)
    assert only_span(exporter).end_time is not None


def test_error_propagation(instrument):
    _, exporter = instrument()
    with sdk(lambda _: httpx.Response(400, json={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'offline-invalid'}})) as client:
        with pytest.raises(anthropic.BadRequestError, match='offline-invalid'):
            client.messages.create(**arguments())
    assert only_span(exporter).status.status_code == StatusCode.ERROR


def test_privacy(instrument):
    _, exporter = instrument(TraceConfig(hide_inputs=True, hide_outputs=True,
                                        hide_input_messages=True, hide_output_messages=True))
    with sdk(lambda _: httpx.Response(200, json=message())) as client:
        assert client.messages.create(**arguments()).content[0].text == ANSWER
    attrs = json.dumps(dict(only_span(exporter).attributes))
    assert SECRET not in attrs
    assert ANSWER not in attrs
