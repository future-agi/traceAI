"""Real Groq SDK -> traceai_groq audio spans -> loopback F01 (same helper as traceai_openai)."""

import asyncio

import pytest

groq = pytest.importorskip("groq")

from conftest import flush, refs, wav  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from traceai_groq import GroqInstrumentor  # noqa: E402

try:
    from groq.resources.audio.speech import Speech  # noqa: F401

    HAS_SPEECH = True
except ImportError:  # groq < 0.1x has no speech endpoint
    HAS_SPEECH = False


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        GroqInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                      config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        GroqInstrumentor().uninstrument()


def client(provider, async_=False):
    cls = groq.AsyncGroq if async_ else groq.Groq
    return cls(api_key="gsk-synthetic", base_url=provider.url, max_retries=0)


@pytest.mark.skipif(not HAS_SPEECH, reason="this groq SDK has no audio.speech")
@pytest.mark.parametrize("async_", [False, True])
def test_speech_buffered_output(f01, provider, instrument, async_):
    manager = f01.manager()
    exporter = instrument(manager)
    request = dict(model="playai-tts", voice="Fritz-PlayAI", input="hi", response_format="wav")
    if async_:
        response = asyncio.run(client(provider, True).audio.speech.create(**request))
    else:
        response = client(provider).audio.speech.create(**request)
    assert response.http_response.content == provider.speech  # unchanged for the application
    flush(manager)
    assert f01.payloads == [provider.speech]
    span = [s for s in exporter.get_finished_spans() if s.name == "Groq Audio Speech"][-1]
    assert [r["purpose"] for r in refs(span)] == ["model_output"]
    manager.shutdown(5)


@pytest.mark.parametrize("operation", ["transcriptions", "translations"])
@pytest.mark.parametrize("async_", [False, True])
def test_transcription_bytes_only(f01, provider, instrument, tmp_path, operation, async_):
    manager = f01.manager()
    instrument(manager)
    audio = wav(seed=21)
    path = tmp_path / "a.wav"
    path.write_bytes(wav(seed=22))

    if async_:
        async def go():
            resource = getattr(client(provider, True).audio, operation)
            await resource.create(model="whisper-large-v3", file=("a.wav", audio))
            await resource.create(model="whisper-large-v3", file=path)
        asyncio.run(go())
    else:
        resource = getattr(client(provider).audio, operation)
        resource.create(model="whisper-large-v3", file=("a.wav", audio))
        resource.create(model="whisper-large-v3", file=path)  # a path is never opened by the adapter
    flush(manager)
    assert f01.payloads == [audio]
    manager.shutdown(5)


def test_privacy_controls_deny(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager, config=TraceConfig(pii_redaction=True))
    client(provider).audio.transcriptions.create(model="whisper-large-v3", file=("a.wav", wav()))
    if HAS_SPEECH:
        client(provider).audio.speech.create(model="playai-tts", voice="v", input="hi")
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_no_manager_leaves_audio_unwrapped(spans):
    from groq.resources.audio.transcriptions import Transcriptions

    tracer_provider, _ = spans
    original = Transcriptions.create
    GroqInstrumentor().instrument(tracer_provider=tracer_provider)
    try:
        assert Transcriptions.create is original
    finally:
        GroqInstrumentor().uninstrument()
