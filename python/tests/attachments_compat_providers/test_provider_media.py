"""provider_media helper rules, through the real manager to a loopback F01 server."""

import builtins
import dataclasses
import io
import os
import subprocess
import sys
import textwrap
import traceback

import pytest
from opentelemetry import context as otel_context

from conftest import MP4, PDF, b64, data_uri, flush, jpeg, png, refs, wav  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, CapturePolicy, create_attachment_manager  # noqa: E402
from fi_instrumentation.attachments import media_capture, provider_media  # noqa: E402
from fi_instrumentation.attachments.provider_media import (  # noqa: E402
    PROVIDER_MEDIA_CAPABILITIES,
    ProviderMedia,
    capabilities,
    consistent,
    sniff,
)

IMAGE = png(seed=31)
OTHER = png(seed=32)


def capture(manager, tracer_provider, fn, *, trace_config=None, name="llm"):
    with tracer_provider.get_tracer("t").start_as_current_span(name) as span:
        media = ProviderMedia(manager, span, "test", trace_config=trace_config)
        fn(media)
    return media, span


def test_compatible_messages_capture_data_uri_images_and_audio(f01, spans):
    tracer_provider, exporter = spans
    manager = f01.manager()
    sound = wav(seed=3)
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": data_uri(IMAGE)}},
            {"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}},
            {"type": "input_audio", "input_audio": {"data": b64(sound), "format": "wav"}},
        ]},
    ]
    media, _ = capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, "openai"))
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, sound])
    assert set(media.captured) == {"chat/1/1", "chat/1/3"}
    span = exporter.get_finished_spans()[-1]
    by_mime = {ref["mime_type"]: ref for ref in refs(span)}
    assert by_mime["image/png"]["message_index"] == 1 and by_mime["image/png"]["part_index"] == 1
    assert by_mime["audio/wav"]["role"] == "user" and by_mime["audio/wav"]["direction"] == "input"
    manager.shutdown(5)


@pytest.mark.parametrize("dialect", ["groq", "mistral", "image"])
def test_image_only_dialects_never_read_audio(f01, spans, dialect):
    tracer_provider, _ = spans
    manager = f01.manager()
    messages = [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": b64(wav()), "format": "wav"}},
        {"type": "image_url", "image_url": data_uri(IMAGE) if dialect == "mistral" else {"url": data_uri(IMAGE)}},
    ]}]
    capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, dialect))
    flush(manager)
    assert f01.payloads == [IMAGE]
    manager.shutdown(5)


def test_wrong_label_is_never_uploaded_and_cannot_bypass_hide_input_images(f01, spans):
    """A PNG labelled as WAV audio would dodge hide_input_images; the magic-byte check refuses it."""
    tracer_provider, _ = spans
    manager = f01.manager()
    messages = [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": b64(IMAGE), "format": "wav"}}]}]
    contents = [{"role": "user", "parts": [{"inline_data": {"mime_type": "audio/wav", "data": OTHER}}]}]
    capture(manager, tracer_provider, lambda m: (m.compatible_messages(messages, "openai"), m.gemini_contents(contents)),
            trace_config=TraceConfig(hide_input_images=True))
    flush(manager)
    assert f01.payloads == []
    assert f01.uploads == []
    manager.shutdown(5)


def test_declared_pdf_and_video_need_matching_bytes(f01, spans):
    tracer_provider, _ = spans
    f01.state["policy"] = {"allowed_modalities": ["audio", "image", "document", "file", "video"]}
    manager = f01.manager()
    contents = [{"role": "user", "parts": [
        {"inline_data": {"mime_type": "application/pdf", "data": PDF}},
        {"inline_data": {"mime_type": "video/mp4", "data": MP4}},
        {"inline_data": {"mime_type": "application/pdf", "data": IMAGE}},  # mislabelled: refused
        {"inline_data": {"mime_type": "image/jpeg", "data": jpeg(4)}},
    ]}]
    capture(manager, tracer_provider, lambda m: m.gemini_contents(contents))
    flush(manager)
    assert sorted(f01.payloads) == sorted([PDF, MP4, jpeg(4)])
    purposes = sorted(u["purpose"] for u in f01.uploads)
    assert purposes == ["document", "model_input", "model_input"]
    manager.shutdown(5)


def test_server_policy_without_video_denies_video(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()  # the harness policy allows audio, image, document and file only
    contents = [{"role": "user", "parts": [{"inline_data": {"mime_type": "video/mp4", "data": MP4}}]}]
    capture(manager, tracer_provider, lambda m: m.gemini_contents(contents))
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_preflight_runs_before_any_read(f01, spans):
    """A denied part is never read: the data-URI getter is not called under hide_input_images."""
    tracer_provider, _ = spans
    manager = f01.manager()
    reads = []

    class Part(dict):
        def get(self, key, default=None):
            if key == "image_url":
                reads.append(key)
            return super().get(key, default)

    messages = [{"role": "user", "content": [Part(type="image_url", image_url={"url": data_uri(IMAGE)})]}]
    capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, "openai"),
            trace_config=TraceConfig(hide_input_images=True))
    flush(manager)
    assert reads == [] and f01.payloads == []
    capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, "openai"))
    flush(manager)
    assert reads == ["image_url"] and f01.payloads == [IMAGE]
    manager.shutdown(5)


def test_small_server_cap_resizes_data_uri_gate(f01, spans):
    """The data-URI gate asks for 1 MiB; a server cap below that re-admits at the exact size."""
    tracer_provider, _ = spans
    f01.state["limits"] = {"max_upload_bytes": 4096}
    manager = f01.manager()
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri(IMAGE)}}]}]
    capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, "openai"))
    flush(manager)
    assert f01.payloads == [IMAGE]
    manager.shutdown(5)


def test_untyped_images_sniff_their_type_and_skip_paths(f01, spans, tmp_path, monkeypatch):
    """Ollama/Titan images: bytes or base64 only. A path string is never opened or decoded into an upload."""
    tracer_provider, _ = spans
    manager = f01.manager()
    path = tmp_path / "picture.png"
    path.write_bytes(OTHER)
    opened = []
    real_open, real_io_open = builtins.open, io.open

    def watch(opener):
        def wrapped(file, *args, **kwargs):
            if any(frame.filename.endswith("provider_media.py") for frame in traceback.extract_stack()):
                opened.append(str(file))
            return opener(file, *args, **kwargs)
        return wrapped

    monkeypatch.setattr(builtins, "open", watch(real_open))
    monkeypatch.setattr(io, "open", watch(real_io_open))
    # A PDF has no image signature: untyped sources are images only, so it is never uploaded.
    images = [IMAGE, b64(jpeg(7)), str(path), path, "not base64!", PDF, b64(PDF)]
    media, _ = capture(manager, tracer_provider, lambda m: m.ollama_images(images))
    flush(manager)
    monkeypatch.undo()
    assert sorted(f01.payloads) == sorted([IMAGE, jpeg(7)])
    assert opened == []
    assert sorted(u["mime_type"] for u in f01.uploads) == ["image/jpeg", "image/png"]
    manager.shutdown(5)


def test_context_policy_and_owner_keys_deny(f01, spans):
    """The shared keys media_capture.CAPTURE_POLICY_KEY / OWNER_KEY apply to provider media (deny wins)."""
    tracer_provider, _ = spans
    manager = f01.manager()
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri(IMAGE)}}]}]
    for key, value in ((media_capture.CAPTURE_POLICY_KEY, CapturePolicy(capture_content=False)),
                       (media_capture.OWNER_KEY, True)):
        token = otel_context.attach(otel_context.set_value(key, value))
        try:
            media, _ = capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, "openai"))
        finally:
            otel_context.detach(token)
        assert media.captured == {}
    flush(manager)
    assert f01.payloads == []
    # claim() marks exactly that owner key.
    assert otel_context.get_value(media_capture.OWNER_KEY, provider_media.claim()) is True
    manager.shutdown(5)


@pytest.mark.parametrize("config", [
    TraceConfig(hide_input_images=True), TraceConfig(hide_inputs=True), TraceConfig(pii_redaction=True),
    TraceConfig(hide_input_messages=True), TraceConfig(hide_input_text=True),
])
def test_local_input_restrictions_deny(f01, spans, config):
    tracer_provider, _ = spans
    manager = f01.manager(allow_unredacted_media_with_text_pii=True)
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri(IMAGE)}}]}]
    contents = [{"role": "user", "parts": [{"inline_data": {"mime_type": "image/png", "data": OTHER}}]}]
    capture(manager, tracer_provider, lambda m: (m.compatible_messages(messages), m.gemini_contents(contents)),
            trace_config=config)
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


@pytest.mark.parametrize("config", [TraceConfig(hide_outputs=True), TraceConfig(hide_output_messages=True),
                                    TraceConfig(hide_output_text=True), TraceConfig(pii_redaction=True)])
def test_local_output_restrictions_deny_generated_media(f01, spans, config):
    tracer_provider, _ = spans
    manager = f01.manager(allow_unredacted_media_with_text_pii=True)
    response = {"candidates": [{"content": {"role": "model", "parts": [
        {"inline_data": {"mime_type": "image/png", "data": IMAGE}}]}}]}
    capture(manager, tracer_provider, lambda m: (m.gemini_response(response),
                                                 m.generated_images([{"b64_json": b64(OTHER)}])),
            trace_config=config)
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_unsampled_disabled_and_missing_managers_capture_nothing(f01, spans):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    manager = f01.manager()
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri(IMAGE)}}]}]
    unsampled = TracerProvider(sampler=ALWAYS_OFF)
    media, _ = capture(manager, unsampled, lambda m: m.compatible_messages(messages))
    assert not media.usable
    disabled = create_attachment_manager(config=AttachmentConfig(enabled=False))
    tracer_provider, _ = spans
    for candidate in (None, disabled, object()):
        media, _ = capture(candidate, tracer_provider, lambda m: m.compatible_messages(messages))
        assert not media.usable and media.captured == {}
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_second_observer_of_same_span_uploads_nothing(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri(IMAGE)}}]}]
    with tracer_provider.get_tracer("t").start_as_current_span("llm") as span:
        first = ProviderMedia(manager, span, "ns").compatible_messages(messages).captured
        second = ProviderMedia(manager, span, "ns").compatible_messages(messages).captured
    flush(manager)
    assert first == second and len(first) == 1 and f01.payloads == [IMAGE]
    manager.shutdown(5)


def test_part_cap_bounds_work(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    calls = []
    preflight = manager.preflight

    def counted(*args, **kwargs):
        calls.append(kwargs.get("mime_type"))
        return preflight(*args, **kwargs)

    manager.preflight = counted
    # 80 parts over two messages: each list is within its own bound, so only the per-observation cap stops it.
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri(png(seed=m * 40 + i))}}
                                             for i in range(40)]} for m in range(2)]
    media, _ = capture(manager, tracer_provider, lambda m: m.compatible_messages(messages))
    flush(manager)
    assert len(f01.payloads) == provider_media.MAX_PARTS == len(media.captured)
    # One preflight per admitted part (exact type and size already match the gate), none past the cap.
    assert len(calls) == provider_media.MAX_PARTS
    del manager.preflight
    media, _ = capture(manager, tracer_provider, lambda m: m.compatible_messages(
        [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": b64(wav(seed=i)), "format": "wav"}}
                                      for i in range(40)]}] * 2))
    flush(manager)
    assert len(media.captured) == provider_media.MAX_PARTS
    manager.shutdown(5)


def test_sniff_and_consistent():
    assert sniff(IMAGE) == "image/png" and sniff(jpeg()) == "image/jpeg" and sniff(PDF) == "application/pdf"
    assert sniff(MP4) == "video/mp4" and sniff(wav()) == "audio/wav" and sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(b"hello") is None and sniff("not bytes") is None
    assert consistent("image/png", IMAGE) and not consistent("audio/wav", IMAGE)
    assert consistent("video/webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 12)
    assert consistent("audio/webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 12)


@pytest.mark.parametrize("brand,expected", [
    (b"isom", "video/mp4"), (b"mp42", "video/mp4"), (b"avc1", "video/mp4"),
    # HEIF/HEIC and AVIF still images, QuickTime and audio-only M4A share the ftyp box: never video/mp4.
    (b"heic", None), (b"heix", None), (b"mif1", None), (b"msf1", None), (b"avif", None), (b"avis", None),
    (b"qt  ", None), (b"M4A ", None), (b"zzzz", None),
])
def test_sniff_checks_iso_bmff_major_brand(brand, expected):
    head = b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x02\x00" + b"\x00" * 16
    assert sniff(head) == expected
    assert consistent("video/mp4", head) is (expected == "video/mp4")


def test_heic_labelled_video_mp4_is_never_uploaded(f01, spans):
    """A HEIC still image declared as video/mp4 would dodge hide_input_images; the brand check refuses it."""
    tracer_provider, _ = spans
    f01.state["policy"] = {"allowed_modalities": ["audio", "image", "document", "file", "video"]}
    manager = f01.manager()
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 24
    contents = [{"role": "user", "parts": [{"inline_data": {"mime_type": "video/mp4", "data": heic}},
                                           {"inline_data": {"mime_type": "video/mp4", "data": MP4}}]}]
    capture(manager, tracer_provider, lambda m: m.gemini_contents(contents),
            trace_config=TraceConfig(hide_input_images=True))
    flush(manager)
    assert f01.payloads == [MP4]
    manager.shutdown(5)


def test_openai_dialect_file_parts_capture_pdf_once(f01, spans):
    """Chat ``file`` (file.file_data) and Responses-style ``input_file`` PDF data URIs; IDs and URLs never read."""
    tracer_provider, exporter = spans
    manager = f01.manager()
    other_pdf = PDF + b"%%second\n"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "summarize"},
        {"type": "file", "file": {"filename": "a.pdf", "file_data": data_uri(PDF, "application/pdf")}},
        {"type": "input_file", "filename": "b.pdf", "file_data": data_uri(other_pdf, "application/pdf")},
        {"type": "file", "file": {"file_id": "file-synthetic"}},
        {"type": "file", "file": {"file_data": data_uri(IMAGE, "application/pdf")}},  # mislabelled: refused
        {"type": "file", "file": {"file_data": data_uri(PDF, "text/plain")}},  # not a captured type
    ]}]
    # hide_input_images does not hide documents.
    # Without documents=True (every route but LiteLLM) no file part is read.
    capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, "openai"))
    flush(manager)
    assert f01.payloads == []
    media, span = capture(manager, tracer_provider,
                          lambda m: m.compatible_messages(messages, "openai", documents=True),
                          trace_config=TraceConfig(hide_input_images=True))
    flush(manager)
    assert sorted(f01.payloads) == sorted([PDF, other_pdf])
    assert set(media.captured) == {"chat/0/1", "chat/0/2"}
    span = exporter.get_finished_spans()[-1]
    assert sorted((r["part_index"], r["purpose"], r["mime_type"]) for r in refs(span)) == [
        (1, "document", "application/pdf"), (2, "document", "application/pdf")]
    manager.shutdown(5)


@pytest.mark.parametrize("dialect", ["groq", "mistral", "image"])
def test_image_only_dialects_never_read_file_parts(f01, spans, dialect):
    tracer_provider, _ = spans
    manager = f01.manager()
    messages = [{"role": "user", "content": [
        {"type": "file", "file": {"file_data": data_uri(PDF, "application/pdf")}},
        {"type": "input_file", "file_data": data_uri(PDF, "application/pdf")}]}]
    capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, dialect, documents=True))
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_data_uri_length_is_bounded_before_slicing(f01, spans):
    """An oversized data URI is refused from its length alone: the string is never sliced or copied."""
    tracer_provider, _ = spans
    manager = f01.manager()
    slices = []

    class Tracked(str):
        def __getitem__(self, key):
            slices.append(key)
            return str.__getitem__(self, key)

    oversized = Tracked("data:image/png;base64," + "A" * (4 * provider_media.MAX_DECODED_BYTES // 3 + 8))
    exact = Tracked(data_uri(IMAGE))
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": oversized}}]}]
    capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, "openai"))
    flush(manager)
    assert f01.payloads == [] and slices == []
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": exact}}]}]
    capture(manager, tracer_provider, lambda m: m.compatible_messages(messages, "openai"))
    flush(manager)
    assert f01.payloads == [IMAGE] and slices  # in-bound values are sliced once, after the bound check
    manager.shutdown(5)


def test_capability_matrix_is_explicit():
    for package in ("traceai_deepseek", "traceai_cerebras", "traceai_together"):
        assert PROVIDER_MEDIA_CAPABILITIES[package]["operations"] == ()
        assert PROVIDER_MEDIA_CAPABILITIES[package]["note"]
    for package, descriptor in PROVIDER_MEDIA_CAPABILITIES.items():
        assert descriptor["schema_version"] == 1 and isinstance(descriptor["operations"], tuple)
        assert "remote_url" in descriptor["unsupported"] or package in (
            "traceai_google_genai", "traceai_vertexai", "traceai_bedrock", "traceai_ollama", "traceai_deepseek",
            "traceai_cerebras")
    assert capabilities("traceai_unknown")["operations"] == ()
    with pytest.raises(TypeError):
        PROVIDER_MEDIA_CAPABILITIES["traceai_xai"] = {}  # read-only


PROBE = textwrap.dedent("""
    import sys
    from opentelemetry.sdk.trace import TracerProvider
    names = sys.argv[1:]
    import importlib
    for name in names:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        instrumentor = [getattr(module, attr) for attr in dir(module) if attr.endswith("Instrumentor")
                        and attr != "BaseInstrumentor"]
        if instrumentor:
            try:
                instrumentor[0]().instrument(tracer_provider=TracerProvider())
            except Exception as error:
                print("SKIP", name, type(error).__name__)
    loaded = sorted(m for m in sys.modules if m.startswith("fi_instrumentation.attachments"))
    print("LOADED", ",".join(loaded))
""")


def test_disabled_path_never_imports_attachment_modules(tmp_path):
    """Without a manager, instrumenting every provider package imports no attachment module."""
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    names = ["traceai_google_genai", "traceai_vertexai", "traceai_bedrock", "traceai_litellm", "traceai_groq",
             "traceai_together", "traceai_fireworks", "traceai_xai", "traceai_mistralai", "traceai_openrouter",
             "traceai_ollama", "traceai_portkey", "traceai_deepseek", "traceai_cerebras", "traceai_vllm",
             "traceai_minimax"]
    result = subprocess.run([sys.executable, str(script), *names], capture_output=True, text=True, timeout=120,
                            env=dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP="True"))
    assert result.returncode == 0, result.stderr[-2000:]
    line = [row for row in result.stdout.splitlines() if row.startswith("LOADED")][-1]
    loaded = line.split(" ", 1)[1] if " " in line else ""
    assert "provider_media" not in loaded and "media_capture" not in loaded, line


def test_dataclass_policy_unchanged_by_helper():
    """local_capture_policy is the only fold; the helper adds no permissive default."""
    policy = media_capture.local_capture_policy(None, direction="input", image=True)
    assert dataclasses.asdict(policy) == dataclasses.asdict(
        dataclasses.replace(CapturePolicy(), model_input=True))


class _Unreadable:
    """A TraceConfig stand-in whose flags cannot be read."""

    def __getattr__(self, name):
        raise RuntimeError(name)


@pytest.mark.parametrize("flags,expected", [
    ({}, {}),
    ({"hide_input_images": True}, {"record_inputs": False}),
    ({"hide_inputs": True}, {"record_inputs": False}),
    ({"hide_input_messages": True}, {"record_inputs": False}),
    ({"hide_input_text": True}, {"record_inputs": False}),
    ({"hide_outputs": True}, {"record_outputs": False}),
    ({"hide_output_text": True}, {"record_outputs": False}),
    ({"pii_redaction": True}, {"capture_content": False}),
])
def test_deferral_policy_folds_local_deny(flags, expected):
    policy = provider_media.deferral_policy(TraceConfig(**flags))
    assert dataclasses.asdict(policy) == dataclasses.asdict(dataclasses.replace(CapturePolicy(), **expected))


def test_deferred_context_merges_and_fails_closed():
    """A deferred call keeps the context policy and owner mark; local deny is added, never relaxed."""
    key = media_capture.CAPTURE_POLICY_KEY
    assert provider_media.deferral_policy(None) == CapturePolicy()
    unreadable = provider_media.deferral_policy(_Unreadable())
    assert not (unreadable.capture_content or unreadable.record_inputs or unreadable.record_outputs)

    outer = otel_context.set_value(media_capture.OWNER_KEY, True,
                                   otel_context.set_value(key, CapturePolicy(record_outputs=False)))
    merged = provider_media.deferred(TraceConfig(hide_input_images=True), outer)
    policy = otel_context.get_value(key, merged)
    assert (policy.record_inputs, policy.record_outputs, policy.capture_content) == (False, False, True)
    assert media_capture.owned_elsewhere(merged)

    garbage = provider_media.deferred(TraceConfig(), otel_context.set_value(key, "not-a-policy"))
    denied = otel_context.get_value(key, garbage)
    assert denied.capture_content is False and denied.requested is False

    token = otel_context.attach(provider_media.deferred(TraceConfig(pii_redaction=True)))
    try:
        assert media_capture.context_capture_policy().capture_content is False
    finally:
        otel_context.detach(token)
    assert media_capture.context_capture_policy() is None
