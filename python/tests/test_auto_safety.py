"""Adversarial regressions for wire privacy, buffering, failures and host-program safety."""

import asyncio
import gzip
import io
import json
import zlib

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from auto_fixture_loader import load_fixtures, request_body, response_chunks, assert_expected
from auto_mock_server import MockServer, sse_body
from fi_instrumentation import FITracer, TraceConfig, using_attributes
from fi_instrumentation.auto import wire
from fi_instrumentation.auto.wire import _core, _privacy, _transports
from fi_instrumentation.auto.wire._capture import Recorder

CHAT = {"model": "gpt", "messages": [{"role": "user", "content": "ping"}]}
URL = "https://api.openai.com/v1/chat/completions"


@pytest.fixture
def recording():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("regression"), config=TraceConfig())
    yield exporter, tracer
    wire.disable()
    provider.shutdown()


def only(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda f: f["name"])
def test_fixtures_through_bounded_recorder(recording, fixture, monkeypatch):
    exporter, tracer = recording
    request, response = fixture["request"], fixture["response"]
    headers, body = request_body(request)
    capture = Recorder(tracer).begin(request["method"], request["url"], headers, body)
    if capture is not None:
        headers, chunks = response_chunks(response)
        capture.on_headers(response["status"], headers)
        for index, chunk in enumerate(chunks):
            if "arrival_offsets_ms" in response:
                import time
                from types import SimpleNamespace
                from fi_instrumentation.auto.wire import _capture
                offset = response["arrival_offsets_ms"][index] / 1000
                capture._start_perf = 0
                # Only the capture's clock is replaced, not the process clock.
                monkeypatch.setattr(_capture, "time", SimpleNamespace(time_ns=time.time_ns, perf_counter=lambda: offset))
            capture.on_chunk(chunk)
        if "error_after_chunks" in response:
            capture.fail(TimeoutError("stream timed out"))
        else:
            capture.finish()
    if not fixture["expect"]["captured"]:
        assert not exporter.get_finished_spans()
        return
    span = only(exporter)
    assert_expected(fixture, span.name, dict(span.attributes), span.status.status_code == trace.StatusCode.OK,
                    span.status.description)


@pytest.mark.parametrize("side,flag", [("input", "hide_inputs"), ("input", "hide_input_text"),
                                        ("output", "hide_outputs"), ("output", "hide_output_text")])
def test_transcript_obeys_its_side(recording, side, flag):
    exporter, base = recording
    tracer = FITracer(base.__wrapped__, config=TraceConfig(**{flag: True}))
    recorder = Recorder(tracer)
    private = "PRIVATE medical history"
    if side == "input":
        capture = recorder.begin("POST", "https://api.openai.com/v1/audio/speech", {},
                                 json.dumps({"input": private, "model": "tts-1"}).encode())
        capture.on_headers(200, {"content-type": "audio/mpeg"})
        capture.on_chunk(b"audio")
    else:
        capture = recorder.begin("POST", "https://api.openai.com/v1/audio/transcriptions", {},
                                 fields={"model": "whisper-1"})
        capture.on_headers(200, {"content-type": "application/json"})
        capture.on_chunk(json.dumps({"text": private}).encode())
    capture.finish()
    attrs = only(exporter).attributes
    assert attrs["gen_ai.audio.transcript"] == "__REDACTED__"
    assert private not in str(dict(attrs))


@pytest.mark.parametrize("key", ["data", "bytes", "b64_json", "file_data", "audio_base64", "images"])
def test_inline_media_scrubbed_in_every_representation(recording, key):
    exporter, tracer = recording
    raw = "AbCd0123" * 80
    value = [raw] if key == "images" else raw
    body = {**CHAT, "media": {key: value}, "tools": [{"name": "media", "schema": {key: value}}]}
    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(body).encode())
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(json.dumps({"choices": [{"message": {"role": "assistant", "content": "data:image/png;base64," + raw}}]}).encode())
    capture.finish()
    assert raw not in str(dict(only(exporter).attributes))
    assert "<omitted: 640 base64 chars>" in only(exporter).attributes["input.value"]


@pytest.mark.parametrize("header", ["Authorization", "xi-api-key", "api-key", "x-goog-api-key",
                                     "anthropic-api-key", "Ocp-Apim-Subscription-Key"])
def test_credentials_scrubbed_from_context_errors_and_arrays(recording, monkeypatch, header):
    exporter, tracer = recording
    secret, fi_secret = "REQUEST_SECRET_123", "FI_SECRET_12345"
    monkeypatch.setenv("FI_SECRET_KEY", fi_secret)
    headers = {header: "Token " + secret if header == "Authorization" else secret}
    body = {**CHAT, "nested": {"authorization_token": "BODY_SECRET_123"},
            "url": "https://private/path?X-Amz-Signature=SIGNED_SECRET_123&sv=2026"}
    with using_attributes(tags=[secret, fi_secret], metadata={"password": "META_SECRET_123"}):
        capture = Recorder(tracer).begin("POST", URL, headers, json.dumps(body).encode())
    capture.on_headers(200, {"content-type": "text/event-stream"})
    capture.on_chunk(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
    capture.fail(TimeoutError(secret + " " + fi_secret))
    span = only(exporter)
    recorded = str(dict(span.attributes)) + str(span.events) + str(span.status.description)
    for forbidden in (secret, fi_secret, "BODY_SECRET_123", "SIGNED_SECRET_123", "META_SECRET_123"):
        assert forbidden not in recorded
    assert span.attributes["output.value"] == "partial"
    assert span.status.status_code == trace.StatusCode.ERROR


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "unknown"])
def test_compressed_bytes_decoded_or_flagged(recording, encoding):
    exporter, tracer = recording
    data = sse_body()
    encoded = gzip.compress(data) if encoding == "gzip" else zlib.compress(data)
    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(CHAT).encode())
    capture.on_headers(200, {"content-type": "text/event-stream", "content-encoding": encoding})
    for index in range(0, len(encoded), 7):
        capture.on_chunk(encoded[index:index + 7])
    capture.finish()
    attrs = only(exporter).attributes
    if encoding == "unknown":
        assert attrs["traceai.wire.undecoded_encoding"] == "unknown"
    else:
        assert attrs["output.value"] == "pong"
        assert attrs["gen_ai.usage.output_tokens"] == 2


def test_decompression_bomb_is_bounded(recording):
    exporter, tracer = recording
    capture = Recorder(tracer, 256).begin("POST", URL, {}, json.dumps(CHAT).encode())
    capture.on_headers(200, {"content-type": "application/json", "content-encoding": "gzip"})
    capture.on_chunk(gzip.compress(b"x" * 100000))
    capture.finish()
    assert only(exporter).attributes["traceai.wire.truncated"]
    assert "output.value" not in only(exporter).attributes


@pytest.mark.parametrize("known", [True, False])
def test_unavailable_and_oversized_requests_require_catalog_host(recording, known):
    exporter, tracer = recording
    recorder = Recorder(tracer, 16)
    url = URL if known else "https://application.example/v1/chat/completions"
    for body in (None, json.dumps(CHAT).encode()):
        capture = recorder.begin("POST", url, {}, body)
        assert bool(capture) == known
        if capture:
            capture.fail(ConnectionError("unreachable"))
    for span in exporter.get_finished_spans():
        assert span.status.status_code == trace.StatusCode.ERROR
        assert "input.value" not in span.attributes


def test_multipart_validation_and_metadata_limits(recording):
    _, tracer = recording
    recorder = Recorder(tracer, 32)
    fields = {"model": "whisper-1", "prompt": "x" * 70000}
    capture = recorder.begin("POST", "https://api.openai.com/v1/audio/transcriptions", {}, fields=fields,
                             files=[_core.FilePart("file", "sample.wav", "audio/wav", 5000000)])
    assert capture is not None
    assert len(json.loads(capture._parsed.input_value)["prompt"]) == 65536
    assert recorder.begin("POST", URL, {}, fields=fields) is None
    assert recorder.begin("POST", "https://api.openai.com/v1/audio/transcriptions", {}, fields={"other": "x"}) is None


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_httpx_gzip_midstream_failure_and_early_close(recording, module):
    httpx = pytest.importorskip(module)
    exporter, tracer = recording
    wire.enable(tracer, transports=[module])

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadTimeout("interrupted")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            with client.stream("POST", URL, json=CHAT) as response:
                list(response.iter_bytes())
        failed = only(exporter)
        assert failed.attributes["output.value"] == "partial"
        assert failed.attributes["error.type"] == "ReadTimeout"
        assert failed.status.status_code == trace.StatusCode.ERROR
        exporter.clear()
        with client.stream("POST", URL, json=CHAT) as response:
            assert next(response.iter_bytes())
        assert only(exporter).attributes["traceai.wire.incomplete"] is True
        assert only(exporter).status.status_code == trace.StatusCode.OK
    exporter.clear()

    class Compressed(httpx.SyncByteStream):
        def __iter__(self):
            yield gzip.compress(sse_body())

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream", "content-encoding": "gzip"}, stream=Compressed()))) as client:
        with client.stream("POST", URL, json=CHAT) as response:
            assert b"".join(response.iter_bytes()) == sse_body()
    assert only(exporter).attributes["output.value"] == "pong"


def test_aiohttp_zero_read_and_close_finalize(recording):
    aiohttp = pytest.importorskip("aiohttp")
    exporter, tracer = recording
    wire.enable(tracer, transports=["aiohttp"])

    async def run(url):
        async with aiohttp.ClientSession() as session:
            response = await session.post(url + "/v1/chat/completions", json=CHAT)
            assert await response.content.read(0) == b""
            assert not exporter.get_finished_spans()
            assert await response.json()
            assert only(exporter).attributes["output.value"] == "pong"
            exporter.clear()
            response = await session.post(url + "/v1/chat/completions", json={**CHAT, "stream": True})
            assert await response.content.read(1)
            response.close()
            span = only(exporter)
            assert span.attributes["traceai.wire.incomplete"] is True
            assert span.status.status_code == trace.StatusCode.OK

    with MockServer() as server:
        asyncio.run(run(server.url))


def test_aiohttp_formdata_is_not_consumed(recording):
    aiohttp = pytest.importorskip("aiohttp")
    _, tracer = recording
    form = aiohttp.FormData()
    form.add_field("model", "whisper-1")
    file = io.BytesIO(b"audio" * 100)
    form.add_field("file", file, filename="long.wav", content_type="audio/wav")
    capture = _transports._aiohttp_begin(Recorder(tracer, 16), "POST",
                                       "https://api.openai.com/v1/audio/transcriptions", {"data": form})
    assert capture is not None
    assert capture._parsed.request_model == "whisper-1"
    assert capture._parsed.extra["traceai.audio.input_bytes"] == 500
    assert file.tell() == 0
    assert len(form._fields) == 2
    assert len(form()._parts) == 2


def test_enabling_more_transports_is_additive(recording):
    httpx = pytest.importorskip("httpx")
    exporter, tracer = recording
    original = httpx.Client.send
    wire.enable(tracer, transports=["httpx"])
    assert httpx.Client.send is not original
    wire.enable(tracer, transports=["requests"])
    assert httpx.Client.send is not original
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []}))) as client:
        client.post(URL, json=CHAT)
    assert len(exporter.get_finished_spans()) == 1


def test_request_stream_is_never_read_ahead(recording):
    httpx = pytest.importorskip("httpx")
    _, tracer = recording

    class OneShot(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("instrumentation read the request stream")

    request = httpx.Request("POST", URL, stream=OneShot())
    capture = _transports._httpx_begin(Recorder(tracer), request)
    assert capture is not None
    assert capture._request.unavailable


def test_extended_content_keys_do_not_become_parameters(recording):
    exporter, tracer = recording
    body = {**CHAT, **{key: "PRIVATE" for key in ("audio_url", "url", "file_url", "image_url", "instructions")}}
    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(body).encode())
    capture.on_headers(200, {})
    capture.finish()
    assert "PRIVATE" not in only(exporter).attributes["gen_ai.request.parameters"]


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_httpx_async_stream_failures_preserve_partial_output(recording, module):
    httpx = pytest.importorskip(module)
    exporter, tracer = recording
    wire.enable(tracer, transports=[module])

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadTimeout("interrupted")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Stream()))) as client:
            with pytest.raises(httpx.ReadTimeout):
                async with client.stream("POST", URL, json=CHAT) as response:
                    async for _ in response.aiter_bytes():
                        pass
            failed = only(exporter)
            assert failed.status.status_code == trace.StatusCode.ERROR
            assert failed.attributes["error.type"] == "ReadTimeout"
            assert failed.attributes["output.value"] == "partial"
            exporter.clear()
            async with client.stream("POST", URL, json=CHAT) as response:
                async for _ in response.aiter_bytes():
                    break
            assert only(exporter).attributes["traceai.wire.incomplete"] is True

    asyncio.run(run())


def test_requests_stream_failures_preserve_partial_output(recording):
    import requests
    from types import SimpleNamespace

    exporter, tracer = recording
    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(CHAT).encode())

    def chunks(*args):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise requests.ReadTimeout("interrupted")

    response = SimpleNamespace(status_code=200, headers={"content-type": "text/event-stream"},
                               _content=False, iter_content=chunks, close=lambda: None)
    _transports._requests_response(capture, response, True)
    with pytest.raises(requests.ReadTimeout):
        list(response.iter_content())
    response.close()
    failed = only(exporter)
    assert failed.status.status_code == trace.StatusCode.ERROR
    assert failed.attributes["error.type"] == "ReadTimeout"
    assert failed.attributes["output.value"] == "partial"


def test_aiohttp_readexactly_failure_retains_partial_bytes(recording):
    exporter, tracer = recording
    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(CHAT).encode())
    capture.on_headers(200, {"content-type": "text/event-stream"})
    partial = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    class Content:
        async def readexactly(self, size):
            raise asyncio.IncompleteReadError(partial, size)

        def at_eof(self):
            return False

    async def run():
        with pytest.raises(asyncio.IncompleteReadError):
            await _transports._ContentTee(Content(), capture).readexactly(1000)

    asyncio.run(run())
    span = only(exporter)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes["output.value"] == "partial"


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text", "hide_outputs", "hide_output_text"])
def test_hide_flags_cover_raw_messages_parameters_and_tool_arguments(recording, flag):
    exporter, base = recording
    tracer = FITracer(base.__wrapped__, config=TraceConfig(**{flag: True}))
    secret = "PRIVATE medical history"
    body = {"model": "gpt", "messages": [{"role": "assistant", "tool_calls": [
        {"id": "t", "type": "function", "function": {"name": "tool", "arguments": json.dumps({"text": secret})}}
    ]}], "extra": secret, "tools": [{"function": {"description": secret}}]}
    capture = Recorder(tracer).begin("POST", URL, {}, json.dumps(body).encode())
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(json.dumps({"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"id": "t", "type": "function", "function": {"name": "tool", "arguments": json.dumps({"text": secret})}}
    ]}}]}).encode())
    capture.finish()
    attrs = only(exporter).attributes
    side = "input" if "input" in flag else "output"
    for key, value in attrs.items():
        if key.startswith(f"gen_ai.{side}.messages") or key == f"{side}.value":
            assert secret not in str(value)
        if side == "input" and (key == "gen_ai.request.parameters" or key.startswith("gen_ai.tool.definitions")):
            assert secret not in str(value)


def test_multipart_unicode_fields_are_capped_in_bytes(recording):
    _, tracer = recording
    capture = Recorder(tracer).begin("POST", "https://api.openai.com/v1/audio/transcriptions", {},
                                     fields={"model": "whisper-1", "prompt": "語" * 65536})
    prompt = json.loads(capture._parsed.input_value)["prompt"]
    assert len(prompt.encode("utf-8")) <= 65536


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_eager_httpx_reads_keep_partial_output_on_timeout(recording, module):
    httpx = pytest.importorskip(module)
    exporter, tracer = recording
    wire.enable(tracer, transports=[module])

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadTimeout("interrupted")

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()))) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.post(URL, json=CHAT)
    span = only(exporter)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes["output.value"] == "partial"


def test_inflight_capture_keeps_request_time_tracer_and_masking(recording):
    exporter, base = recording
    hidden = FITracer(base.__wrapped__, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    recorder = Recorder(hidden)
    capture = recorder.begin("POST", URL, {}, json.dumps(CHAT).encode())
    recorder.tracer = base
    capture.on_headers(200, {"content-type": "application/json"})
    capture.on_chunk(b'{"choices":[{"message":{"role":"assistant","content":"PRIVATE"}}]}')
    capture.finish()
    assert only(exporter).attributes["output.value"] == "__REDACTED__"


def test_requests_response_hooks_keep_original_stream_argument(recording):
    import requests

    exporter, tracer = recording
    wire.enable(tracer, transports=["requests"])
    observed = []
    with MockServer() as server:
        response = requests.Session().post(server.url + "/v1/chat/completions", json=CHAT,
                                           hooks={"response": lambda response, **kwargs: observed.append(kwargs["stream"])})
        assert response.json()["choices"][0]["message"]["content"] == "pong"
    assert observed == [False]
    assert only(exporter).attributes["output.value"] == "pong"


def test_multipart_regular_file_size_does_not_move_cursor(recording, tmp_path):
    httpx = pytest.importorskip("httpx")
    _, tracer = recording
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio" * 100)
    with path.open("rb") as file:
        file.read(5)
        request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions",
                                data={"model": "whisper-1"}, files={"file": ("audio.wav", file, "audio/wav")})
        capture = _transports._httpx_begin(Recorder(tracer, 16), request)
        assert file.tell() == 5
        assert capture._parsed.extra["traceai.audio.input_bytes"] == 495
