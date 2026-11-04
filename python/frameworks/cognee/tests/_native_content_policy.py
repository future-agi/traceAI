"""Isolated real Cognee clients -> loopback HTTP/SSE -> loopback OTLP probe."""
import asyncio
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading

from fi_instrumentation.instrumentation.config import TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_cognee import configure
from traceai_cognee._structured import redact_structured

# The shared repository receiver decodes actual OTLP protobuf requests.
sys.path.insert(0, sys.argv[1])
from harness import Receiver, _flatten_attributes  # noqa: E402

PROVIDER = sys.argv[2]
CAPTURE = sys.argv[3] == 'true'
MODEL = 'llama3.2:3b' if PROVIDER == 'ollama' else 'openai/gpt-4o-mini'
INPUT_EMAIL = 'synthetic.input@example.com'
OUTPUT_EMAIL = 'synthetic.output@example.com'
PROMPT = json.dumps({'question': 'Contact ' + INPUT_EMAIL, 'api_key': 'synthetic-input-secret'})
OUTPUT = json.dumps({'answer': 'Contact ' + OUTPUT_EMAIL, 'password': 'synthetic-output-secret'})


async def exercise(endpoint):
    if PROVIDER == 'ollama':
        from cognee.infrastructure.llm.structured_output_framework.litellm_instructor.llm.ollama.adapter import OllamaAPIAdapter
        adapter = OllamaAPIAdapter(endpoint=endpoint, api_key='synthetic-provider-key',
                                   model=MODEL, name='Ollama', max_completion_tokens=50)
        try:
            assert await adapter.acreate_structured_output(PROMPT, 'synthetic system', str) == OUTPUT
        finally:
            await adapter.client.close()
        return

    from cognee.infrastructure.llm.structured_output_framework.litellm_instructor.llm.openai.adapter import OpenAIAdapter
    from cognee.infrastructure.llm.streaming.token_sink import TokenSink, active_token_sink
    adapter = OpenAIAdapter(endpoint=endpoint, api_key='synthetic-provider-key',
                            model=MODEL, max_completion_tokens=50)
    for streaming in (False, True):
        sink = TokenSink() if streaming else None
        token = active_token_sink.set(sink)
        try:
            assert await adapter.acreate_structured_output(
                PROMPT, 'synthetic system', str, num_retries=0
            ) == OUTPUT
        finally:
            active_token_sink.reset(token)
            if sink:
                sink.close()


def main():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            if self.path != '/v1/chat/completions':
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            base = {'id': 'chatcmpl-synthetic', 'created': 1, 'model': body['model']}
            usage = {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}
            if body.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                chunks = [
                    {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': OUTPUT},
                                  'finish_reason': None}]},
                    {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
                    {'choices': [], 'usage': usage},
                ]
                for chunk in chunks:
                    data = {**base, 'object': 'chat.completion.chunk', **chunk}
                    self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
                    self.wfile.flush()
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
                self.close_connection = True
            else:
                data = json.dumps({**base, 'object': 'chat.completion', 'usage': usage,
                                   'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': OUTPUT},
                                                'finish_reason': 'stop'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with Receiver() as receiver:
            handle = configure(project_name='native-content-policy', capture_content=CAPTURE,
                               endpoint=receiver.collector_endpoint,
                               headers={'X-Api-Key': 'synthetic-api', 'X-Secret-Key': 'synthetic-secret'})
            raw = InMemorySpanExporter()
            handle.provider.add_span_processor(SimpleSpanProcessor(raw))
            try:
                asyncio.run(exercise(f'http://127.0.0.1:{server.server_port}/v1'))
                assert handle.force_flush()
                exported = receiver.spans()
                native = raw.get_finished_spans()
                expected_count = 1 if PROVIDER == 'ollama' else 2
                assert len(exported) == len(native) == len(requests) == expected_count
                assert sum(bool(r.get('stream')) for r in requests) == (PROVIDER == 'openai')
                assert all(INPUT_EMAIL in json.dumps(r) for r in requests)
                config = TraceConfig()
                assert config.pii_redaction == (os.environ['FI_PII_REDACTION'] == 'true')
                wire = json.dumps(exported)
                raw_wire = encode_spans(native).SerializeToString()
                for marker in (INPUT_EMAIL, OUTPUT_EMAIL, 'synthetic-input-secret', 'synthetic-output-secret'):
                    assert marker.encode() in raw_wire, 'parallel exporter must retain native content'
                for secret in ('synthetic-input-secret', 'synthetic-output-secret', 'synthetic-provider-key'):
                    assert secret not in wire
                by_id = {span.context.span_id: span for span in native}
                for span in exported:
                    source = by_id[int.from_bytes(b64decode(span['spanId']), 'big')]
                    assert int.from_bytes(b64decode(span['traceId']), 'big') == source.context.trace_id
                    attrs = _flatten_attributes(span['attributes'])
                    assert attrs['fi.span.kind'] == 'LLM'
                    for direction, marker, hidden in (
                        ('input', INPUT_EMAIL, config.hide_inputs),
                        ('output', OUTPUT_EMAIL, config.hide_outputs),
                    ):
                        key = 'langfuse.observation.' + direction
                        assert marker in source.attributes[key]
                        visible = CAPTURE and not hidden
                        assert (key in attrs) == visible
                        assert (direction + '.value' in attrs) == visible
                        assert (direction + '.mime_type' in attrs) == visible
                        assert (marker in wire) == (visible and not config.pii_redaction)
                        if visible:
                            expected = config.mask(key, redact_structured(source.attributes[key]))
                            assert attrs[key] == expected
                            assert attrs[direction + '.value'] == expected
                            assert attrs[direction + '.mime_type'] == 'application/json'
                            assert ('<EMAIL_ADDRESS>' in expected) == config.pii_redaction
                    assert source.attributes['gen_ai.request.model'] == MODEL
                    assert attrs.get('gen_ai.request.model') == MODEL, 'valid tagged model must survive'
                assert all(r['path'] == '/tracer/v1/traces' for r in receiver.requests())
            finally:
                handle.shutdown()
                handle.provider.shutdown()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
