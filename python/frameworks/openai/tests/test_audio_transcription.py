"""Audio contracts through the real SDK and a localhost OTLP/protobuf receiver.

All vendor traffic uses MockTransport; run this suite with outbound traffic denied.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from traceai_openai import OpenAIInstrumentor

TEXT = "synthetic private transcript"
PROMPT = "synthetic private vocabulary"
AUDIO = b"synthetic private audio"
MODEL = "gpt-4o-transcribe"
USAGE = {"type": "tokens", "input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
         "input_token_details": {"audio_tokens": 5, "text_tokens": 0}}


@pytest.fixture
def wire():
    payloads = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", headers={}, timeout=2,
    )))
    instrumentor = OpenAIInstrumentor()

    def start(config=None):
        instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
        return payloads

    try:
        yield start
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def only_span(payloads):
    spans = [span for payload in payloads
             for resource in ExportTraceServiceRequest.FromString(payload).resource_spans
             for scope in resource.scope_spans for span in scope.spans]
    assert len(spans) == 1
    span = spans[0]
    attrs = {a.key: getattr(a.value, a.value.WhichOneof("value")) for a in span.attributes}
    return span, attrs


def sdk(handler, use_async):
    transport = httpx.MockTransport(handler)
    kwargs = {"api_key": "fake-offline-key", "base_url": "https://api.openai.com/v1",
              "max_retries": 0}
    if use_async:
        return openai.AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(
            transport=transport, trust_env=False))
    return openai.OpenAI(**kwargs, http_client=httpx.Client(transport=transport, trust_env=False))


def params(**extra):
    return {"model": MODEL, "file": ("synthetic.wav", AUDIO, "audio/wav"),
            "prompt": PROMPT, **extra}


def assert_metadata(attrs):
    assert attrs["gen_ai.operation.name"] == "transcription"
    assert attrs["gen_ai.request.model"] == MODEL
    assert attrs["gen_ai.provider.name"] == "openai"
    assert AUDIO.decode() not in json.dumps(attrs)


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("usage", [USAGE, {**USAGE, "input_tokens": 0, "output_tokens": 0,
                                           "total_tokens": 0}, {"type": "duration", "seconds": 12}, None])
@pytest.mark.parametrize("response_format", ["json", "verbose_json", "diarized_json"])
def test_transcription_usage_on_wire(wire, use_async, usage, response_format):
    if response_format == "diarized_json" and not hasattr(openai.types.audio, "TranscriptionDiarized"):
        pytest.skip("diarized_json is not available at the SDK floor")
    payloads = wire()
    body = {"text": TEXT, "task": "transcribe", "language": "en", "duration": 12,
            "segments": []}
    if usage is not None:
        body["usage"] = usage

    def handler(request):
        assert request.url.path == "/v1/audio/transcriptions"
        assert AUDIO in request.content
        return httpx.Response(200, json=body)

    async def run_async():
        async with sdk(handler, True) as client:
            result = await client.audio.transcriptions.create(**params(response_format=response_format))
            assert result.text == TEXT

    if use_async:
        asyncio.run(run_async())
    else:
        with sdk(handler, False) as client:
            result = client.audio.transcriptions.create(**params(response_format=response_format))
            assert result.text == TEXT
    span, attrs = only_span(payloads)
    assert span.status.code == 1  # OK
    assert_metadata(attrs)
    actual = {key: value for key, value in attrs.items() if key.startswith("gen_ai.usage.")}
    if usage and usage["type"] == "tokens":
        assert actual == {f"gen_ai.usage.{name}": usage[name]
                          for name in ("input_tokens", "output_tokens", "total_tokens")}
    else:
        assert actual == {}  # Duration must never be treated as a token count.


def stream_response():
    events = [
        {"type": "transcript.text.delta", "delta": "synthetic ", "logprobs": []},
        {"type": "transcript.text.done", "text": TEXT, "usage": USAGE, "logprobs": []},
    ]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("mode", ["full", "early", "empty", "close", "error", "cancel"])
def test_transcription_stream_lifecycle(wire, use_async, mode):
    payloads = wire()

    def consume(event):
        if mode == "error":
            raise ValueError("consumer stopped")
        if mode == "cancel":
            raise asyncio.CancelledError()
        return mode == "early"

    async def run_async():
        async with sdk(lambda _: stream_response(), True) as client:
            stream = await client.audio.transcriptions.create(**params(stream=True))
            assert not payloads  # Do not finish at HTTP headers or pre-read audio events.
            if mode == "close":
                await stream.close()
                await stream.close()
            else:
                try:
                    async with stream as entered:
                        assert entered is stream
                        if mode != "empty":
                            async for event in entered:
                                if consume(event):
                                    break
                except (ValueError, asyncio.CancelledError):
                    assert mode in ("error", "cancel")
            assert stream.response.is_closed

    def run_sync():
        with sdk(lambda _: stream_response(), False) as client:
            stream = client.audio.transcriptions.create(**params(stream=True))
            assert not payloads
            if mode == "close":
                stream.close()
                stream.close()
            else:
                try:
                    with stream as entered:
                        assert entered is stream
                        if mode != "empty":
                            for event in entered:
                                if consume(event):
                                    break
                except (ValueError, asyncio.CancelledError):
                    assert mode in ("error", "cancel")
            assert stream.response.is_closed

    asyncio.run(run_async()) if use_async else run_sync()
    span, attrs = only_span(payloads)
    assert_metadata(attrs)
    assert span.status.code == (2 if mode in ("error", "cancel") else 1)
    if mode == "full":
        assert attrs["gen_ai.usage.input_tokens"] == 5
        assert attrs["gen_ai.usage.output_tokens"] == 2
        assert attrs["gen_ai.usage.total_tokens"] == 7
        assert attrs["output.value"] == TEXT
        assert attrs["output.mime_type"] == "text/plain"
    else:
        assert not any(key.startswith("gen_ai.usage.") for key in attrs)


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_transcription_privacy_keeps_usage(wire, use_async, stream):
    payloads = wire(TraceConfig(hide_inputs=True, hide_outputs=True))
    handler = lambda _: stream_response() if stream else httpx.Response(
        200, json={"text": TEXT, "usage": USAGE})

    async def run_async():
        async with sdk(handler, True) as client:
            result = await client.audio.transcriptions.create(**params(stream=stream))
            if stream:
                async with result:
                    async for _ in result:
                        pass
            else:
                assert result.text == TEXT

    if use_async:
        asyncio.run(run_async())
    else:
        with sdk(handler, False) as client:
            result = client.audio.transcriptions.create(**params(stream=stream))
            if stream:
                with result:
                    list(result)
            else:
                assert result.text == TEXT
    _, attrs = only_span(payloads)
    assert_metadata(attrs)
    assert attrs["gen_ai.usage.total_tokens"] == 7
    for secret in (TEXT, PROMPT, AUDIO.decode(), "fake-offline-key"):
        assert secret.encode() not in b"".join(payloads)


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("echo_input", [False, True])
def test_transcription_error_retains_operation(wire, use_async, echo_input):
    if echo_input and not hasattr(TraceConfig(), "hide_error_details"):
        pytest.skip("error-detail privacy requires fi-core's hide_error_details support")
    payloads = wire(TraceConfig(hide_inputs=True, hide_outputs=True))
    handler = lambda _: httpx.Response(400, json={
        "error": {"message": PROMPT if echo_input else "invalid audio"}})

    async def run_async():
        async with sdk(handler, True) as client:
            await client.audio.transcriptions.create(**params())

    with pytest.raises(openai.BadRequestError):
        if use_async:
            asyncio.run(run_async())
        else:
            with sdk(handler, False) as client:
                client.audio.transcriptions.create(**params())
    span, attrs = only_span(payloads)
    assert span.status.code == 2
    assert_metadata(attrs)
    assert PROMPT.encode() not in b"".join(payloads)
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)


@pytest.mark.parametrize("usage", [
    {}, {"type": "unknown", "input_tokens": 5},
    {"type": "duration", "seconds": 12, "total_tokens": 99},
    {"type": "tokens", "input_tokens": -1, "output_tokens": True, "total_tokens": "7"},
])
def test_invalid_usage_does_not_invent_token_counts(usage):
    from openai.types.audio import Transcription
    from traceai_openai._response_attributes_extractor import _ResponseAttributesExtractor

    # Bypass model validation to model an incomplete OpenAI-compatible response.
    response = Transcription.model_construct(text=TEXT)
    response.usage = usage
    attrs = dict(_ResponseAttributesExtractor(openai).get_attributes_from_response(response, {}))
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)


@pytest.mark.parametrize("use_async", [False, True])
def test_transcription_stream_api_error(wire, use_async):
    payloads = wire()
    body = 'data: {"error": {"message": "synthetic stream failure"}}\n\n'

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    async def run_async():
        async with sdk(handler, True) as client:
            stream = await client.audio.transcriptions.create(**params(stream=True))
            async with stream:
                async for _ in stream:
                    pass

    with pytest.raises(openai.APIError, match="synthetic stream failure"):
        if use_async:
            asyncio.run(run_async())
        else:
            with sdk(handler, False) as client:
                with client.audio.transcriptions.create(**params(stream=True)) as stream:
                    list(stream)
    span, attrs = only_span(payloads)
    assert span.status.code == 2
    assert_metadata(attrs)
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)
