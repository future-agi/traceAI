"""Real OpenAI SDK -> traceai_openai -> loopback F01: images, files, screenshots and speech."""

import asyncio
import json
import threading

import pytest

openai = pytest.importorskip("openai")

from conftest import PDF, b64, flush, png, refs, wait_uploads, wav  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF  # noqa: E402
from traceai_openai import OpenAIInstrumentor  # noqa: E402

SDK = tuple(int(x) for x in openai.__version__.split(".")[:2])
IMAGE = png(seed=1)
SCREENSHOT = png(seed=2)


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None, **kwargs):
        OpenAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                        config=config or TraceConfig(), **kwargs)
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        OpenAIInstrumentor().uninstrument()


def client(provider, async_=False):
    cls = openai.AsyncOpenAI if async_ else openai.OpenAI
    return cls(api_key="sk-synthetic", base_url=provider.url + "/v1", max_retries=0)


def chat_request(stream=False):
    return dict(model="gpt-test", stream=stream, messages=[
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(IMAGE)}},
            {"type": "image_url", "image_url": {"url": "https://example.invalid/remote.png"}},
            {"type": "file", "file": {"filename": "a.pdf", "file_data": "data:application/pdf;base64," + b64(PDF)}},
        ]},
    ])


def run_chat(provider, async_, stream):
    if async_:
        async def go():
            result = await client(provider, True).chat.completions.create(**chat_request(stream))
            if stream:
                async for _ in result:
                    pass
        asyncio.run(go())
    else:
        result = client(provider).chat.completions.create(**chat_request(stream))
        if stream:
            for _ in result:
                pass


def llm_span(exporter, name=None):
    found = [s for s in exporter.get_finished_spans() if name is None or s.name == name]
    assert found, [s.name for s in exporter.get_finished_spans()]
    return found[-1]


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_chat_image_and_file_captured_and_projected(f01, provider, instrument, async_, stream):
    manager = f01.manager()
    exporter = instrument(manager)
    run_chat(provider, async_, stream)
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, PDF])
    uploads = {u["mime_type"]: u for u in f01.uploads}
    assert set(uploads) == {"image/png", "application/pdf"}
    assert uploads["image/png"]["purpose"] == "model_input" and uploads["image/png"]["direction"] == "input"
    assert uploads["application/pdf"]["purpose"] == "document"
    span = llm_span(exporter)
    span_refs = refs(span)
    assert {(r["mime_type"], r["message_index"], r["part_index"], r["role"]) for r in span_refs} == {
        ("image/png", 1, 1, "user"), ("application/pdf", 1, 3, "user")}
    exported = json.dumps(dict(span.attributes))
    assert b64(IMAGE) not in exported and b64(PDF) not in exported
    for ref in span_refs:
        assert "fi-attachment:" + ref["occurrence_id"] in exported
    # The SDK request itself is unchanged: the provider still received the original media.
    sent = provider.requests[-1][1]
    assert sent["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64," + b64(IMAGE)
    assert sent["messages"][1]["content"][2]["image_url"]["url"] == "https://example.invalid/remote.png"
    manager.shutdown(5)


def responses_input():
    return [
        {"role": "user", "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64," + b64(IMAGE)},
            {"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64," + b64(PDF)},
        ]},
        {"type": "computer_call", "id": "cu_1", "call_id": "call_cu1", "status": "completed",
         "action": {"type": "screenshot"}, "pending_safety_checks": []},
        {"type": "computer_call_output", "call_id": "call_cu1",
         "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64," + b64(SCREENSHOT)}},
    ]


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_responses_inputs_screenshot_and_generated_image(f01, provider, instrument, async_, stream):
    manager = f01.manager()
    exporter = instrument(manager)
    request = dict(model="computer-use-test", input=responses_input(), stream=stream,
                   tools=[{"type": "image_generation"}])

    if async_:
        async def go():
            result = await client(provider, True).responses.create(**request)
            if stream:
                async for _ in result:
                    pass
        asyncio.run(go())
    else:
        result = client(provider).responses.create(**request)
        if stream:
            for _ in result:
                pass
    flush(manager)
    # Input image, input PDF, the computer screenshot and the COMPLETED generated image; never the partial.
    assert sorted(f01.payloads) == sorted([IMAGE, PDF, SCREENSHOT, provider.image])
    assert png(seed=3) not in f01.payloads
    by_purpose = {u["purpose"]: u for u in f01.uploads}
    assert set(by_purpose) == {"model_input", "document", "computer_screenshot", "model_output"}
    assert by_purpose["computer_screenshot"]["tool_call_id"] == "call_cu1"
    assert by_purpose["model_output"]["direction"] == "output"
    span = llm_span(exporter)
    screenshot = [r for r in refs(span) if r.get("purpose") == "computer_screenshot"]
    assert screenshot and screenshot[0]["role"] == "tool" and screenshot[0]["message_index"] == 2
    exported = json.dumps(dict(span.attributes))
    for data in (IMAGE, PDF, SCREENSHOT):
        assert b64(data) not in exported
    manager.shutdown(5)


@pytest.mark.parametrize("async_", [False, True])
def test_images_generate_b64_output(f01, provider, instrument, async_):
    manager = f01.manager()
    instrument(manager)
    if async_:
        asyncio.run(client(provider, True).images.generate(model="gpt-image-1", prompt="a dot"))
    else:
        client(provider).images.generate(model="gpt-image-1", prompt="a dot")
    flush(manager)
    assert f01.payloads == [provider.image]  # the url-only item is never fetched
    assert f01.uploads[0]["mime_type"] == "image/png" and f01.uploads[0]["purpose"] == "model_output"
    manager.shutdown(5)


@pytest.mark.parametrize("async_", [False, True])
def test_speech_buffered_output(f01, provider, instrument, async_):
    manager = f01.manager()
    instrument(manager)
    if async_:
        result = asyncio.run(client(provider, True).audio.speech.create(
            model="tts-1", voice="alloy", input="hi", response_format="wav"))
    else:
        result = client(provider).audio.speech.create(model="tts-1", voice="alloy", input="hi",
                                                      response_format="wav")
    assert result.content == provider.speech  # the application's result is unchanged
    flush(manager)
    assert f01.payloads == [provider.speech]
    assert f01.uploads[0]["mime_type"] == "audio/wav" and f01.uploads[0]["direction"] == "output"
    manager.shutdown(5)


@pytest.mark.parametrize("mode", ["stream_to_file", "iter_bytes", "read"])
def test_speech_streamed_consumed_output(f01, provider, instrument, tmp_path, mode):
    manager = f01.manager()
    instrument(manager)
    path = tmp_path / "out.wav"
    with client(provider).audio.speech.with_streaming_response.create(
            model="tts-1", voice="alloy", input="hi") as response:
        if mode == "stream_to_file":
            response.stream_to_file(path)
            assert path.read_bytes() == provider.speech
        elif mode == "iter_bytes":
            assert b"".join(response.iter_bytes(100)) == provider.speech
        else:
            assert response.read() == provider.speech
    flush(manager)
    assert f01.payloads == [provider.speech]
    manager.shutdown(5)


def test_speech_streamed_async_stream_to_file(f01, provider, instrument, tmp_path):
    manager = f01.manager()
    instrument(manager)
    path = tmp_path / "out.wav"

    async def go():
        async with client(provider, True).audio.speech.with_streaming_response.create(
                model="tts-1", voice="alloy", input="hi") as response:
            await response.stream_to_file(path)
    asyncio.run(go())
    assert path.read_bytes() == provider.speech
    flush(manager)
    assert f01.payloads == [provider.speech]
    manager.shutdown(5)


def test_speech_partial_read_uploads_nothing(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager)
    with client(provider).audio.speech.with_streaming_response.create(
            model="tts-1", voice="alloy", input="hi") as response:
        iterator = response.iter_bytes(64)
        next(iterator)
        iterator.close()  # the application stopped early
    flush(manager)
    assert f01.payloads == [] and f01.uploads == []
    manager.shutdown(5)


def test_speech_declared_format_mismatch_uploads_nothing(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager)
    client(provider).audio.speech.create(model="tts-1", voice="alloy", input="hi", response_format="mp3")
    flush(manager)
    assert f01.payloads == []  # WAV bytes labelled mp3 are never mislabelled
    manager.shutdown(5)


@pytest.mark.parametrize("operation", ["transcriptions", "translations"])
def test_transcription_input_bytes_and_paths_only(f01, provider, instrument, tmp_path, operation):
    manager = f01.manager()
    instrument(manager)
    audio = wav(seed=4)
    resource = getattr(client(provider).audio, operation)
    resource.create(model="whisper-1", file=("a.wav", audio, "audio/wav"))
    path = tmp_path / "b.wav"
    path.write_bytes(wav(seed=5))
    resource.create(model="whisper-1", file=path)  # the SDK itself reads a path into bytes
    with open(path, "rb") as handle:
        resource.create(model="whisper-1", file=handle)  # an open file is the app's stream: not read
    flush(manager)
    assert f01.payloads == [audio, wav(seed=5)]
    assert all(u["direction"] == "input" and u["purpose"] == "model_input" for u in f01.uploads)
    manager.shutdown(5)


# ------------------------------------------------------------ privacy controls
@pytest.mark.parametrize("config", [
    TraceConfig(hide_input_images=True), TraceConfig(hide_inputs=True), TraceConfig(pii_redaction=True),
    TraceConfig(hide_input_messages=True), TraceConfig(hide_input_text=True),
])
def test_local_hide_and_pii_deny_input_media_including_screenshots(f01, provider, instrument, config):
    # Even a manager that would relax text PII for media cannot relax a local restriction.
    manager = f01.manager(allow_unredacted_media_with_text_pii=True)
    instrument(manager, config=config)
    client(provider).responses.create(model="m", input=responses_input())
    client(provider).chat.completions.create(**chat_request())
    flush(manager)
    payloads = f01.payloads
    assert SCREENSHOT not in payloads and IMAGE not in payloads
    if not config.hide_input_images:
        assert PDF not in payloads
    manager.shutdown(5)


def test_hide_outputs_denies_generated_media_and_speech(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager, config=TraceConfig(hide_outputs=True))
    client(provider).images.generate(model="gpt-image-1", prompt="x")
    client(provider).audio.speech.create(model="tts-1", voice="alloy", input="hi")
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_server_policy_audio_only_denies_images(f01, provider, instrument):
    f01.state["policy"] = {"allowed_modalities": ["audio"]}
    manager = f01.manager()
    instrument(manager)
    client(provider).chat.completions.create(**chat_request())
    flush(manager)
    assert f01.payloads == [] and f01.after_bootstrap() == []
    manager.shutdown(5)


def test_unsampled_span_uploads_nothing(f01, provider):
    manager = f01.manager()
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(sampler=ALWAYS_OFF)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager)
    try:
        client(provider).chat.completions.create(**chat_request())
    finally:
        OpenAIInstrumentor().uninstrument()
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_disabled_manager_is_a_strict_no_op(provider, spans):
    tracer_provider, exporter = spans
    manager = create_attachment_manager(config=AttachmentConfig(enabled=False))
    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager)
    try:
        client(provider).chat.completions.create(**chat_request())
        wrapper = openai.OpenAI.request
        assert getattr(wrapper, "_self_wrapper", None) is None or \
            getattr(wrapper._self_wrapper, "_attachments", None) is None
    finally:
        OpenAIInstrumentor().uninstrument()
    # The loopback server and HTTP client own their threads; no attachment thread exists.
    assert not [t.name for t in threading.enumerate() if t.name.startswith("fi-attachment")]
    span = llm_span(exporter)
    assert "fi.attachment.refs" not in span.attributes
    assert "fi-attachment:" not in json.dumps(dict(span.attributes))


def test_env_disable_wins_over_enabled_config(f01, provider, instrument, monkeypatch):
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
    manager = create_attachment_manager(config=AttachmentConfig(enabled=True), project_name="synthetic",
                                        transport=f01.transport())
    instrument(manager)
    client(provider).chat.completions.create(**chat_request())
    assert f01.state["requests"] == []


def test_second_observation_returns_first_occurrence(f01, spans):
    from traceai_openai import capture_openai_attachments

    tracer_provider, _ = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    payload = chat_request()
    with tracer.start_as_current_span("llm") as span:
        first = capture_openai_attachments(manager, span, payload, operation="chat", direction="input")
        second = capture_openai_attachments(manager, span, payload, operation="chat", direction="input")
    flush(manager)
    assert first == second and len(first) == 2
    assert len(f01.payloads) == 2
    manager.shutdown(5)


def test_projection_returns_same_object_without_capture():
    from traceai_openai import project_openai_media

    payload = chat_request()
    assert project_openai_media(payload, {}, "chat", "input") is payload


def test_malformed_parts_fail_closed_without_upload(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager)
    bad = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,@@@@"}},
        {"type": "image_url", "image_url": {"url": "data:image/gif;base64," + b64(IMAGE)}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(IMAGE)[:-1]}},
        {"type": "input_file", "file_id": "file-abc"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(b"\x00" * (300 * 1024))}},
    ]
    client(provider).chat.completions.create(model="m", messages=[{"role": "user", "content": bad}])
    flush(manager)
    assert f01.payloads == []
    assert manager.status().open_tickets == 0 and manager.owned_bytes == 0
    manager.shutdown(5)
