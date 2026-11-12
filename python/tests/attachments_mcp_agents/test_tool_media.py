"""The shared tool-media helper: deny-wins folding, preflight before read, budgets, dedup, ownership."""

import struct

import pytest

from fi_instrumentation import TraceConfig
from fi_instrumentation.attachments import AttachmentConfig, CapturePolicy, create_attachment_manager
from fi_instrumentation.attachments import tool_media
from opentelemetry import context as otel_context

from _harness import b64, data_uri, flush, png, refs


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_messages", "hide_input_text", "hide_outputs",
                                  "hide_output_messages", "hide_output_text", "pii_redaction"])
def test_tool_policy_folds_input_and_output_restrictions(flag):
    policy = tool_media.tool_capture_policy(TraceConfig(**{flag: True}))
    assert policy.capture_content is False and policy.model_input and policy.contains_output


def test_tool_policy_images_and_errors():
    assert tool_media.tool_capture_policy(TraceConfig(hide_input_images=True), image=True).capture_content is False
    assert tool_media.tool_capture_policy(TraceConfig(hide_input_images=True)).capture_content is True
    assert tool_media.tool_capture_policy(TraceConfig(hide_error_details=True), error=True).capture_content is False
    base = CapturePolicy(record_outputs=False)
    assert tool_media.tool_capture_policy(None, base).record_outputs is False


def test_owner_context_disables_capture(f01, spans):
    manager = f01.manager()
    provider, _ = spans
    token = otel_context.attach(tool_media.with_ownership())
    try:
        with provider.get_tracer("t").start_as_current_span("owned") as span:
            media = tool_media.ToolMedia(manager, span, "test")
            assert not media.usable
            assert tool_media.tool_capture_policy().requested is False
    finally:
        otel_context.detach(token)
    manager.shutdown(5)


def test_value_is_read_only_after_admission_and_only_once(f01, spans):
    manager = f01.manager()
    provider, _ = spans
    reads = []

    def read():
        reads.append(1)
        return b64(png(seed=1))

    with provider.get_tracer("t").start_as_current_span("tool") as span:
        denied = tool_media.ToolMedia(manager, span, "test", trace_config=TraceConfig(hide_outputs=True))
        assert denied.base64("a", read, "image/png", purpose="tool_result", expect="image") is None
        assert reads == []
        media = tool_media.ToolMedia(manager, span, "test")
        first = media.base64("b", read, "image/png", purpose="tool_result", expect="image")
        again = tool_media.ToolMedia(manager, span, "test").base64("b", read, "image/png", purpose="tool_result")
        assert first is not None and again == first and reads == [1]
        assert media.base64("c", read, "audio/wav", purpose="tool_result", expect="image") is None
        assert media.base64("d", read, "image/gif", purpose="tool_result") is None
        assert reads == [1]
    flush(manager)
    assert len(f01.uploads) == 1 and len(refs(span)) == 1
    manager.shutdown(5)


def test_budget_and_part_cap(f01, spans):
    manager = f01.manager()
    provider, _ = spans
    with provider.get_tracer("t").start_as_current_span("tool") as span:
        media = tool_media.ToolMedia(manager, span, "test", budget=200, max_parts=2)
        image = png(width=4, height=4, seed=2)
        assert len(image) < 200
        assert media.base64("1", lambda: b64(image), "image/png", purpose="tool_result") is not None
        assert media.remaining == 200 - len(image)
        assert media.data_uri("2", lambda: data_uri("image/png", png(width=40, height=40, seed=3)), ("image/png",),
                              purpose="tool_result") is None  # over the remaining budget
        assert media.base64("3", lambda: b64(png(seed=4)), "image/png", purpose="tool_result") is None  # part cap
    flush(manager)
    assert len(f01.uploads) == 1
    manager.shutdown(5)


def test_disabled_and_missing_managers_are_inert(spans):
    provider, _ = spans
    with provider.get_tracer("t").start_as_current_span("tool") as span:
        for manager in (None, create_attachment_manager(config=AttachmentConfig())):
            media = tool_media.ToolMedia(manager, span, "test")
            assert not media.usable
            assert media.base64("x", lambda: pytest.fail("read"), "image/png", purpose="tool_result") is None


def test_pil_images_are_encoded_only_when_in_memory(f01, spans, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    manager = f01.manager()
    provider, _ = spans
    in_memory = Image.new("RGB", (4, 3), (1, 2, 3))
    path = tmp_path / "x.png"
    in_memory.save(path)
    with provider.get_tracer("t").start_as_current_span("tool") as span:
        media = tool_media.ToolMedia(manager, span, "test")
        assert media.image("file", Image.open(path), purpose="tool_result") is None
        assert media.image("mem", in_memory, purpose="tool_result") is not None
        assert media.image("huge", Image.new("L", (4096, 4096)), purpose="tool_result") is None
        assert media.image("not-an-image", b"\x89PNG", purpose="tool_result") is None
    flush(manager)
    assert [u["mime_type"] for u in f01.uploads] == ["image/png"]
    manager.shutdown(5)


def test_signature_of_another_modality_is_denied_and_never_relabelled(f01, spans):
    """A PNG declared as audio, a generic file or a PDF is not uploaded (hide_input_images cannot be escaped)."""
    from _harness import PDF, wav

    manager = f01.manager()
    provider, _ = spans
    shot = png(seed=21)
    with provider.get_tracer("t").start_as_current_span("tool") as span:
        media = tool_media.ToolMedia(manager, span, "test", trace_config=TraceConfig(hide_input_images=True))
        assert media.base64("a", lambda: b64(shot), "audio/wav", purpose="tool_result", expect="audio") is None
        assert media.base64("b", lambda: b64(shot), "application/octet-stream", purpose="tool_result") is None
        assert media.base64("c", lambda: b64(shot), "application/pdf", purpose="tool_result") is None
        assert media.data_uri("d", lambda: "data:audio/wav;base64," + b64(shot), ("audio/wav",),
                              purpose="tool_result") is None
        # Matching signatures and signature-less bytes still upload.
        assert media.base64("e", lambda: b64(wav()), "audio/wav", purpose="tool_result", expect="audio")
        assert media.base64("f", lambda: b64(PDF), "application/pdf", purpose="tool_result")
    flush(manager)
    assert shot not in f01.payloads
    assert sorted(u["mime_type"] for u in f01.uploads) == ["application/pdf", "audio/wav"]
    manager.shutdown(5)


@pytest.mark.parametrize("head,kind", [
    (b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00", "image"), (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01", "image"),
    (b"RIFF\x00\x00\x00\x00WEBP", "image"), (b"RIFF\x00\x00\x00\x00WAVE", "audio"), (b"OggS\x00\x02", "audio"),
    (b"fLaC\x00\x00", "audio"), (b"ID3\x04\x00", "audio"), (b"%PDF-1.7\n", "document"),
    (b"\x00\x00\x00\x18ftypmp42", None), (b"\x1aE\xdf\xa3\x9fB\x86", None), (b"RIFF\x00\x00\x00\x00AVI ", None),
    (b"plain text", None), (b"BM6\x00\x00\x00\x00\x00\x00\x006\x00", "image"), (b"II*\x00\x08\x00\x00\x00", "image"),
    (b"MM\x00*\x00\x00\x00\x08", "image"), (b"\x00\x00\x00\x18ftypavif", "image"),
    (b"\x00\x00\x00\x18ftypheic", "image"), (b"\x00\x00\x00\x18ftypheix", "image"),
    (b"\x00\x00\x00\x18ftypmif1", "image"), (b"\x00\x00\x00\x18ftypmsf1", "image"),
    (b"\x00\x00\x00\x18ftypisom", None), (b"\x00\x00\x00\x18ftypM4A ", None), (b"\x00\x00\x00\x14ftypqt  ", None),
    (b"\x00\x00\x00\x18avifftyp", None)])
def test_signature_kind(head, kind):
    assert tool_media.signature_kind(head) == kind


def _bmp():
    pixels = b"\x00\x00\xff\x00" * 4
    header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
    return header + struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 32, 0, len(pixels), 2835, 2835, 0, 0) + pixels


def _tiff(order=b"II*\x00"):
    return order + struct.pack("<I", 8) + b"\x00" * 40


def _heif(brand):
    return struct.pack(">I", 24) + b"ftyp" + brand + b"\x00\x00\x00\x00" + brand + b"mif1" + b"\x00" * 32


_IMAGE_MISLABELS = [
    ("bmp-as-audio/wav", "audio/wav", _bmp), ("bmp-as-audio/L16", "audio/L16", _bmp),
    ("bmp-as-octet-stream", "application/octet-stream", _bmp), ("tiff-le-as-audio/L16", "audio/L16", _tiff),
    ("tiff-be-as-audio/wav", "audio/wav", lambda: _tiff(b"MM\x00*")),
    ("avif-as-video/mp4", "video/mp4", lambda: _heif(b"avif")), ("heic-as-audio/mp4", "audio/mp4", lambda: _heif(b"heic")),
    ("heix-as-octet-stream", "application/octet-stream", lambda: _heif(b"heix")),
    ("mif1-as-video/mp4", "video/mp4", lambda: _heif(b"mif1")), ("msf1-as-video/mp4", "video/mp4", lambda: _heif(b"msf1"))]


@pytest.mark.parametrize("label,mime,make", _IMAGE_MISLABELS, ids=[case[0] for case in _IMAGE_MISLABELS])
def test_bmp_tiff_and_heif_images_cannot_escape_hide_input_images(f01, spans, label, mime, make):
    """Image bytes declared as audio, video or a generic file are not uploaded under hide_input_images."""
    manager = f01.manager()
    provider, _ = spans
    data = make()
    with provider.get_tracer("t").start_as_current_span("tool") as span:
        media = tool_media.ToolMedia(manager, span, "test", trace_config=TraceConfig(hide_input_images=True))
        assert media.base64("a", lambda: b64(data), mime, purpose="tool_result") is None
        assert media.data_uri("b", lambda: "data:%s;base64,%s" % (mime, b64(data)), (mime,),
                              purpose="tool_result") is None
    flush(manager)
    assert f01.uploads == [] and data not in f01.payloads
    manager.shutdown(5)


def test_octet_stream_with_a_recognised_signature_is_skipped(f01, spans):
    """application/octet-stream is the file modality: image, audio or PDF bytes in it are skipped, never relabelled."""
    from _harness import PDF, wav

    manager = f01.manager()
    provider, _ = spans
    plain = b"opaque tool output, no known signature" * 4
    with provider.get_tracer("t").start_as_current_span("tool") as span:
        media = tool_media.ToolMedia(manager, span, "test")
        for position, data in (("png", png(seed=31)), ("wav", wav()), ("pdf", PDF), ("bmp", _bmp())):
            assert media.base64(position, lambda d=data: b64(d), "application/octet-stream",
                                purpose="tool_result") is None, position
        assert media.base64("plain", lambda: b64(plain), "application/octet-stream", purpose="tool_result")
    flush(manager)
    assert [(u["mime_type"], p) for u, p in zip(f01.uploads, f01.payloads)] == [("application/octet-stream", plain)]
    manager.shutdown(5)
