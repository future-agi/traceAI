"""The shared helper: preflight-before-read, strict decode, deny-wins folding, consumed audio."""

import base64

import pytest
from conftest import b64, flush, png, wav
from fi_instrumentation import TraceConfig
from fi_instrumentation.attachments import CapturePolicy
from fi_instrumentation.attachments.media_capture import (
    STREAM_RESERVATIONS,
    ConsumedAudio,
    attach_inline,
    audio_mime,
    local_capture_policy,
    sniff_audio,
    split_inline,
    strict_decode,
)


def test_sniff_and_declared_format_cross_check():
    assert sniff_audio(wav()) == "audio/wav"
    assert sniff_audio(b"OggS" + b"\x00" * 8) == "audio/ogg"
    assert sniff_audio(b"fLaC" + b"\x00" * 8) == "audio/flac"
    assert sniff_audio(b"ID3\x04" + b"\x00" * 8) == "audio/mpeg"
    assert sniff_audio(b"\xff\xfb\x90\x00" + b"\x00" * 8) == "audio/mpeg"
    assert sniff_audio(b"\xff\xf1\x50\x80" + b"\x00" * 8) is None  # AAC ADTS has no F01 MIME
    assert sniff_audio(b"\x00" * 12) is None
    assert audio_mime(wav(), "wav") == "audio/wav"
    assert audio_mime(wav(), "mp3") is None
    assert audio_mime(wav(), "pcm") is None


def test_split_and_strict_decode_are_bounded_and_canonical():
    data = png()
    assert split_inline("data:image/png;base64," + b64(data), ("image/png",), False)[0] == "image/png"
    assert split_inline("data:image/gif;base64," + b64(data), ("image/png",), False) is None
    assert split_inline("data:image/png;base64,abc", ("image/png",), False) is None  # not a multiple of 4
    assert split_inline("x" * (400 * 1024), ("image/png",), True) is None
    assert strict_decode(b64(data), len(data)) == data
    assert strict_decode(b64(data), len(data) - 1) is None  # the ticket budget is a hard bound
    assert strict_decode("@@@@", 10) is None
    non_canonical = base64.b64encode(b"ab").decode()[:-2] + "x="  # bad padding bits
    assert strict_decode(non_canonical, 10) is None


def test_local_restrictions_fold_deny_only():
    assert local_capture_policy(None, direction="input").capture_content is True
    assert local_capture_policy(None, direction="input").model_input is True
    for name in ("hide_inputs", "hide_input_messages", "hide_input_text", "pii_redaction"):
        assert local_capture_policy(TraceConfig(**{name: True}), direction="input").capture_content is False
    assert local_capture_policy(TraceConfig(hide_input_images=True), direction="input", image=True) \
        .capture_content is False
    assert local_capture_policy(TraceConfig(hide_input_images=True), direction="input").capture_content is True
    assert local_capture_policy(TraceConfig(hide_outputs=True), direction="output").capture_content is False
    assert local_capture_policy(TraceConfig(hide_outputs=True), direction="input").capture_content is True
    # A caller base that already denies stays denied.
    assert local_capture_policy(None, direction="input", base=CapturePolicy(capture_content=False)) \
        .capture_content is False


def test_denied_source_is_never_read(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    reads = []

    def read():
        reads.append(1)
        return "data:image/png;base64," + b64(png())

    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        denied = attach_inline(manager, span, read, ("image/png",), raw=False, direction="input",
                               purpose="model_input", capture=CapturePolicy(capture_content=False))
        assert denied is None and reads == []
        admitted = attach_inline(manager, span, read, ("image/png",), raw=False, direction="input",
                                 purpose="model_input", capture=CapturePolicy())
        assert admitted is not None and reads == [1]
    flush(manager)
    assert f01.payloads == [png()]
    manager.shutdown(5)


def test_consumed_audio_steps_reservation_below_server_cap(f01, spans):
    f01.state["limits"] = {"max_upload_bytes": 1024 * 1024}
    tracer_provider, _ = spans
    manager = f01.manager()
    audio = wav(frames=4000)
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        observer = ConsumedAudio(manager, span, direction="output", purpose="model_output",
                                 capture=CapturePolicy())
        for offset in range(0, len(audio), 5):
            observer.write(audio[offset:offset + 5])
        assert observer.finish() is not None
    flush(manager)
    assert f01.payloads == [audio]
    assert STREAM_RESERVATIONS[0] > 1024 * 1024
    manager.shutdown(5)


def test_consumed_audio_abort_and_unknown_container_upload_nothing(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        aborted = ConsumedAudio(manager, span, direction="output", purpose="model_output", capture=CapturePolicy())
        aborted.write(wav())
        aborted.abort()
        assert aborted.finish() is None
        unknown = ConsumedAudio(manager, span, direction="output", purpose="model_output", capture=CapturePolicy())
        unknown.write(b"\x00" * 64)
        assert unknown.finish() is None and unknown.reason == "source_unavailable"
    flush(manager)
    assert f01.payloads == []
    assert manager.owned_bytes == 0
    manager.shutdown(5)
