"""First real google-genai SDK contract: mocked HTTP, never mocked Models methods.

These are baseline execution checks, not floor/latest or release qualification.
"""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

from google import genai
from google.genai import errors, types
import httpx
import pytest
from opentelemetry.trace import StatusCode
from fi_instrumentation import TraceConfig
from traceai_google_genai import GoogleGenAIInstrumentor

MODEL = 'gemini-contract-model'
SECRET = 'private-contract-input'
ANSWER = 'offline answer'


def response(text=ANSWER):
    return {'candidates': [{'content': {'parts': [{'text': text}], 'role': 'model'},
                            'finishReason': 'STOP', 'index': 0}],
            'usageMetadata': {'promptTokenCount': 4, 'candidatesTokenCount': 2, 'totalTokenCount': 6},
            'modelVersion': MODEL}


@contextmanager
def client(handler):
    # 1.8.0 has no public transport-injection or Client context-manager API.
    # A loopback HTTP server exercises that floor without replacing SDK methods.
    class Server(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('content-length', '0')))
            result = handler(None)
            self.send_response(result.status_code)
            for key, value in result.headers.items():
                self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(result.content)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sdk = genai.Client(api_key='offline-not-a-key', http_options=types.HttpOptions(
        base_url=f'http://127.0.0.1:{server.server_port}'))
    try:
        yield sdk
    finally:
        if hasattr(sdk, 'close'):
            sdk.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def instrument(tracing):
    provider, exporter = tracing
    instrumentor = GoogleGenAIInstrumentor()

    def start(config=None):
        instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
        assert instrumentor.is_instrumented_by_opentelemetry
        return provider, exporter

    yield start
    instrumentor.uninstrument()


def only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [s.name for s in spans]
    assert spans[0].end_time is not None
    return spans[0]


def test_sync_return_parent_model_usage(instrument):
    provider, exporter = instrument()
    with provider.get_tracer('contract').start_as_current_span('parent') as parent:
        with client(lambda request: httpx.Response(200, json=response())) as sdk:
            result = sdk.models.generate_content(model=MODEL, contents=SECRET)
        assert result.text == ANSWER
        span = only_span(exporter)
        assert span.parent.span_id == parent.get_span_context().span_id
    assert span.status.status_code == StatusCode.OK
    attrs = dict(span.attributes)
    assert MODEL in str(attrs)
    assert attrs['gen_ai.usage.input_tokens'] == 4
    assert attrs['gen_ai.usage.output_tokens'] == 2


def test_async_and_streaming_execute_real_sdk(instrument):
    _, exporter = instrument()

    async def run():
        with client(lambda request: httpx.Response(200, json=response())) as sdk:
            result = await sdk.aio.models.generate_content(model=MODEL, contents='q')
            assert result.text == ANSWER
    asyncio.run(run())
    assert only_span(exporter).status.status_code == StatusCode.OK
    exporter.clear()
    body = 'data: ' + json.dumps(response()) + '\n\n'
    with client(lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)) as sdk:
        chunks = list(sdk.models.generate_content_stream(model=MODEL, contents='q'))
    assert ''.join(c.text or '' for c in chunks) == ANSWER
    assert only_span(exporter).end_time is not None


def test_sdk_error_propagates_and_span_finishes(instrument):
    _, exporter = instrument()
    with client(lambda request: httpx.Response(400, json={'error': {'code': 400, 'message': 'offline-invalid', 'status': 'INVALID_ARGUMENT'}})) as sdk:
        with pytest.raises(errors.ClientError, match='offline-invalid'):
            sdk.models.generate_content(model=MODEL, contents='q')
    assert only_span(exporter).status.status_code == StatusCode.ERROR


def test_privacy_hides_request_and_response(instrument):
    _, exporter = instrument(TraceConfig(hide_inputs=True, hide_outputs=True,
                                        hide_input_messages=True, hide_output_messages=True))
    with client(lambda request: httpx.Response(200, json=response())) as sdk:
        assert sdk.models.generate_content(model=MODEL, contents=SECRET).text == ANSWER
    span = only_span(exporter)
    attrs = json.dumps(dict(span.attributes))
    assert SECRET not in attrs
    assert ANSWER not in attrs
