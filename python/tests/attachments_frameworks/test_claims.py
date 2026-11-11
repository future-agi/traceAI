"""Shared helper: cross-span claims, raw bytes, URL policy and part limits (no framework installed)."""

import hashlib
import http.server
import threading

import pytest
from conftest import b64, flush, jpeg, png, refs, wav
from fi_instrumentation import TraceConfig
from fi_instrumentation.attachments import CapturePolicy
from fi_instrumentation.attachments import media_capture as mc
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.sdk.trace import TracerProvider

IMAGE = png(seed=11)
URI = "data:image/png;base64," + b64(IMAGE)


def _inline(manager, span, value=URI, policy=None, raw=False):
    return mc.attach_inline(manager, span, lambda: value, mc.IMAGE_MIMES, raw=raw, direction="input",
                            purpose="model_input", capture=policy or mc.local_capture_policy(None, direction="input"),
                            message_index=0, part_index=0, role="user")


def test_framework_then_provider_span_uploads_identical_bytes_once(f01, spans):
    tracer_provider, exporter = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("framework") as parent:
        first = _inline(manager, parent)
        with tracer.start_as_current_span("provider") as child:
            second = _inline(manager, child)
    flush(manager)
    assert first is not None and second == first
    assert f01.payloads == [IMAGE]
    names = {span.name: refs(span) for span in exporter.get_finished_spans()}
    assert [r["occurrence_id"] for r in names["framework"]] == [first.occurrence_id]
    assert names["provider"] == []  # the occurrence stays bound to the first span


def test_raw_bytes_and_inline_base64_of_the_same_image_share_one_claim(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        a = mc.attach_bytes(manager, span, IMAGE, mc.IMAGE_MIMES, direction="input", purpose="model_input",
                            capture=CapturePolicy(model_input=True), message_index=0, part_index=0)
        b = _inline(manager, span)
    flush(manager)
    assert a == b and f01.payloads == [IMAGE]


def test_separate_traces_on_one_manager_share_the_claim(f01, spans):
    # LangChain and LlamaIndex do not make their span current, so the provider span is a separate root.
    tracer_provider, _ = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("framework") as one:
        a = _inline(manager, one)
    with tracer.start_as_current_span("provider") as two:
        b = _inline(manager, two)
    flush(manager)
    assert a == b and f01.payloads == [IMAGE]


def test_another_manager_or_an_expired_claim_uploads_again(f01, spans, monkeypatch):
    tracer_provider, _ = spans
    first, second = f01.manager(), f01.manager()
    tracer = tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("s") as span:
        a = _inline(first, span)
        b = _inline(second, span)
    now = mc._clock()
    monkeypatch.setattr(mc, "_clock", lambda: now + mc.CLAIM_TTL + 1)
    with tracer.start_as_current_span("later") as span:
        c = _inline(first, span)
    flush(first)
    flush(second)
    assert len({a, b, c}) == 3 and f01.payloads == [IMAGE, IMAGE, IMAGE]


def test_a_later_observer_denied_by_its_own_policy_gets_nothing(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    hidden = mc.local_capture_policy(TraceConfig(hide_input_images=True), direction="input", image=True)
    with tracer.start_as_current_span("framework") as parent:
        assert _inline(manager, parent) is not None
        with tracer.start_as_current_span("provider") as child:
            assert _inline(manager, child, policy=hidden) is None  # reuse never widens permission
    flush(manager)
    assert f01.payloads == [IMAGE]


def test_denied_bytes_are_never_hashed(f01, spans, monkeypatch):
    tracer_provider, _ = spans
    manager = f01.manager()
    calls = []
    real = hashlib.sha256
    monkeypatch.setattr(mc.hashlib, "sha256", lambda data=b"": calls.append(1) or real(data))
    hidden = mc.local_capture_policy(TraceConfig(hide_inputs=True), direction="input")
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        assert _inline(manager, span, policy=hidden) is None
        assert mc.attach_bytes(manager, span, IMAGE, mc.IMAGE_MIMES, direction="input", purpose="model_input",
                               capture=hidden) is None
    assert calls == [] and f01.after_bootstrap() == []


def test_concurrent_observers_of_identical_bytes_upload_once(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    results = []
    with tracer.start_as_current_span("root") as root:
        def observe():
            with tracer.start_as_current_span("child", context=None) as child:
                results.append(_inline(manager, child))
        from opentelemetry import context as otel_context
        ctx = otel_context.get_current()
        threads = [threading.Thread(target=lambda: (otel_context.attach(ctx), observe())) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    flush(manager)
    assert len(set(results)) == 1 and None not in results
    assert f01.payloads == [IMAGE]


def test_magic_bytes_must_match_and_select_the_exact_type(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    policy = CapturePolicy(model_input=True)
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        assert mc.attach_bytes(manager, span, b"not an image at all", mc.IMAGE_MIMES, direction="input",
                               purpose="model_input", capture=policy) is None
        assert mc.attach_bytes(manager, span, wav(), mc.IMAGE_MIMES, direction="input",
                               purpose="model_input", capture=policy) is None  # audio is not an image
        found = mc.attach_bytes(manager, span, jpeg(), mc.IMAGE_MIMES, direction="input", purpose="model_input",
                                capture=policy, part_index=1)
        assert found is not None and found.mime_type == "image/jpeg"
        # verify=True on inline data: a PNG data URI holding JPEG bytes is rejected.
        lying = "data:image/png;base64," + b64(jpeg(seed=3))
        assert mc.attach_inline(manager, span, lambda: lying, mc.IMAGE_MIMES, raw=False, direction="input",
                                purpose="model_input", capture=policy, verify=True) is None
    flush(manager)
    assert f01.payloads == [jpeg()]


def test_over_cap_part_is_denied_before_any_read(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    reads = []
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (mc.MAX_FRAMEWORK_BYTES + 1)
    part = mc.MediaPart("bytes", lambda: reads.append(1) or big, mc.IMAGE_MIMES, "model_input", 0, 0)
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        assert mc.capture_parts(manager, span, "test", [part], direction="input") == {}
    flush(manager)
    assert f01.payloads == []


def test_url_parts_go_to_core_policy_and_are_never_fetched(f01, spans):
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tracer_provider, _ = spans
        manager = f01.manager()
        urls = ["http://127.0.0.1:%d/private.png" % server.server_port, "http://169.254.169.254/latest",
                "file:///etc/passwd"]
        parts = [mc.MediaPart("url", lambda u=u: u, mc.IMAGE_MIMES, "model_input", 0, i) for i, u in enumerate(urls)]
        with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
            assert mc.capture_parts(manager, span, "test", parts, direction="input") == {}
        flush(manager)
        assert hits == [] and f01.after_bootstrap() == []
    finally:
        server.shutdown()
        server.server_close()


def test_part_limit_per_span(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    parts = [mc.MediaPart("inline", lambda i=i: b64(png(seed=i)), mc.IMAGE_MIMES, "model_input", 0, i)
             for i in range(mc.MAX_FRAMEWORK_PARTS + 6)]
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        captured = mc.capture_parts(manager, span, "test", parts, direction="input")
    flush(manager)
    assert len(captured) == mc.MAX_FRAMEWORK_PARTS
    assert len(f01.payloads) == mc.MAX_FRAMEWORK_PARTS


def test_unsampled_span_captures_nothing(f01):
    manager = f01.manager()
    tracer = TracerProvider(sampler=ALWAYS_OFF).get_tracer("t")
    with tracer.start_as_current_span("s") as span:
        assert _inline(manager, span) is None
    assert f01.after_bootstrap() == []


def test_intersection_is_deny_wins():
    a = CapturePolicy()
    b = CapturePolicy(capture_content=False, muted=True)
    both = mc.intersect(a, b)
    assert both.capture_content is False and both.muted is True and both.record_inputs is True
    assert mc.intersect().capture_content is True
    # Any configuration in the list that hides wins (FI and OpenInference configs, for example).
    class OIConfig:
        hide_inputs = False
        hide_input_images = True
    folded = mc.intersect(*(mc.local_capture_policy(c, direction="input", image=True)
                            for c in (TraceConfig(), OIConfig())))
    assert folded.capture_content is False and folded.model_input is True


def test_capture_parts_folds_every_config(f01, spans):
    class OIConfig:
        hide_input_images = True
    tracer_provider, _ = spans
    manager = f01.manager()
    parts = [mc.MediaPart("inline", lambda: URI, mc.IMAGE_MIMES, "model_input", 0, 0, raw=False)]
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        assert mc.capture_parts(manager, span, "test", parts, direction="input",
                                trace_configs=(TraceConfig(), OIConfig())) == {}
        assert mc.capture_parts(manager, span, "test", parts, direction="input",
                                trace_configs=(TraceConfig(),))
    flush(manager)
    assert f01.payloads == [IMAGE]


def test_looks_base64():
    assert mc.looks_base64(b64(IMAGE).encode())
    assert not mc.looks_base64(IMAGE)
    assert not mc.looks_base64(b"%PDF-1.4 abc")
    assert not mc.looks_base64("not bytes")


@pytest.mark.parametrize("value", [None, 7, b"", "data:image/png;base64,@@@@"])
def test_malformed_sources_capture_nothing(f01, spans, value):
    tracer_provider, _ = spans
    manager = f01.manager()
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        assert _inline(manager, span, value=value) is None
    flush(manager)
    assert f01.payloads == []
