"""Shared helper: per-span occurrences, raw bytes, URL policy and part limits (no framework installed).

F01 resolves an occurrence only under its own parent span, so every observing span owns
its own occurrence: nothing here reuses an occurrence across spans or traces.
"""

import http.server
import threading

import pytest
from conftest import b64, flush, jpeg, png, refs, wav
from fi_instrumentation import TraceConfig
from fi_instrumentation.attachments import CapturePolicy
from fi_instrumentation.attachments import media_capture as mc
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

IMAGE = png(seed=11)
URI = "data:image/png;base64," + b64(IMAGE)


def _inline(manager, span, value=URI, policy=None, raw=False, part_index=0):
    return mc.attach_inline(manager, span, lambda: value, mc.IMAGE_MIMES, raw=raw, direction="input",
                            purpose="model_input", capture=policy or mc.local_capture_policy(None, direction="input"),
                            message_index=0, part_index=part_index, role="user")


def _parents(f01):
    return [body["association"]["parent"]["span_id"] for body in f01.uploads]


def test_framework_and_provider_spans_each_own_their_occurrence(f01, spans):
    tracer_provider, exporter = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("framework") as parent:
        first = _inline(manager, parent)
        with tracer.start_as_current_span("provider") as child:
            second = _inline(manager, child)
    flush(manager)
    assert first is not None and second is not None and first != second
    assert f01.payloads == [IMAGE, IMAGE]  # byte deduplication is the server's job
    names = {span.name: refs(span) for span in exporter.get_finished_spans()}
    assert [r["occurrence_id"] for r in names["framework"]] == [first.occurrence_id]
    assert [r["occurrence_id"] for r in names["provider"]] == [second.occurrence_id]
    # Upload workers run concurrently, so the order the server sees them in is not fixed.
    assert sorted(_parents(f01)) == sorted(["%016x" % parent.get_span_context().span_id,
                                            "%016x" % child.get_span_context().span_id])


def test_raw_bytes_and_inline_base64_at_two_positions_are_two_occurrences(f01, spans):
    tracer_provider, exporter = spans
    manager = f01.manager()
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        a = mc.attach_bytes(manager, span, IMAGE, mc.IMAGE_MIMES, direction="input", purpose="model_input",
                            capture=CapturePolicy(model_input=True), message_index=0, part_index=0)
        b = _inline(manager, span, part_index=1)
    flush(manager)
    assert a is not None and b is not None and a != b
    assert f01.payloads == [IMAGE, IMAGE]
    (span,) = exporter.get_finished_spans()
    assert sorted(r["occurrence_id"] for r in refs(span)) == sorted([a.occurrence_id, b.occurrence_id])


def test_separate_traces_on_one_manager_never_share_an_occurrence(f01, spans):
    # LangChain and LlamaIndex do not make their span current, so the provider span is a separate root.
    tracer_provider, exporter = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("framework") as one:
        a = _inline(manager, one)
    with tracer.start_as_current_span("provider") as two:
        b = _inline(manager, two)
    flush(manager)
    assert one.get_span_context().trace_id != two.get_span_context().trace_id
    assert a is not None and b is not None and a != b and f01.payloads == [IMAGE, IMAGE]
    for span, occurrence in zip(exporter.get_finished_spans(), (a, b)):
        assert [r["occurrence_id"] for r in refs(span)] == [occurrence.occurrence_id]


def test_same_span_and_position_observed_twice_is_one_occurrence(f01, spans):
    # The (span, position) registry still dedups ONE span observing ONE position twice.
    tracer_provider, exporter = spans
    manager = f01.manager()
    part = mc.MediaPart("inline", lambda: URI, mc.IMAGE_MIMES, "model_input", 0, 0, raw=False)
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        first = mc.capture_parts(manager, span, "test", [part], direction="input")
        again = mc.capture_parts(manager, span, "test", [part], direction="input")
    flush(manager)
    assert dict(first) == dict(again) and f01.payloads == [IMAGE]
    (span,) = exporter.get_finished_spans()
    assert len(refs(span)) == 1


class _FailingStore(list):
    """The loopback server fails every content PUT while this is its payload store."""

    def append(self, item):
        raise RuntimeError("synthetic content PUT failure")


def test_a_failed_upload_is_not_reused_by_a_later_span(f01, spans):
    # The first content PUT fails; the same bytes on a later span upload again under that span.
    tracer_provider, exporter = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    good = f01.state["payloads"]
    f01.state["payloads"] = _FailingStore()
    with tracer.start_as_current_span("first") as one:
        _inline(manager, one)
    manager.flush(15)
    f01.state["payloads"] = good
    with tracer.start_as_current_span("second") as two:
        b = _inline(manager, two)
    flush(manager)
    assert b is not None and f01.payloads == [IMAGE]
    second = [s for s in exporter.get_finished_spans() if s.name == "second"][0]
    assert [r["occurrence_id"] for r in refs(second)] == [b.occurrence_id]
    assert _parents(f01)[-1] == "%016x" % two.get_span_context().span_id


def test_a_later_observer_denied_by_its_own_policy_gets_nothing(f01, spans):
    tracer_provider, exporter = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    hidden = mc.local_capture_policy(TraceConfig(hide_input_images=True), direction="input", image=True)
    with tracer.start_as_current_span("framework") as parent:
        assert _inline(manager, parent) is not None
        with tracer.start_as_current_span("provider") as child:
            assert _inline(manager, child, policy=hidden) is None
    flush(manager)
    assert f01.payloads == [IMAGE]
    assert refs([s for s in exporter.get_finished_spans() if s.name == "provider"][0]) == []


def test_denied_sources_are_never_read(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    reads = []
    hidden = mc.local_capture_policy(TraceConfig(hide_inputs=True), direction="input")
    with tracer_provider.get_tracer("t").start_as_current_span("s") as span:
        assert mc.attach_inline(manager, span, lambda: reads.append(1) or URI, mc.IMAGE_MIMES, raw=False,
                                direction="input", purpose="model_input", capture=hidden) is None
        part = mc.MediaPart("bytes", lambda: reads.append(1) or IMAGE, mc.IMAGE_MIMES, "model_input", 0, 1)
        assert mc.capture_parts(manager, span, "test", [part], direction="input",
                                trace_configs=(TraceConfig(hide_inputs=True),)) == {}
    assert reads == [] and f01.after_bootstrap() == []


def test_concurrent_observers_of_identical_bytes_each_own_one(f01, spans):
    tracer_provider, exporter = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    results = []
    with tracer.start_as_current_span("root"):
        ctx = otel_context.get_current()

        def observe():
            otel_context.attach(ctx)
            with tracer.start_as_current_span("child") as child:
                results.append(_inline(manager, child))

        threads = [threading.Thread(target=observe) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    flush(manager)
    assert None not in results and len(set(results)) == 8
    assert f01.payloads == [IMAGE] * 8
    children = [s for s in exporter.get_finished_spans() if s.name == "child"]
    assert sorted(r["occurrence_id"] for s in children for r in refs(s)) == \
        sorted(o.occurrence_id for o in results)


def test_tool_policy_folds_input_and_output_hide_flags(f01, spans):
    tracer_provider, _ = spans
    manager = f01.manager()
    part = mc.MediaPart("bytes", lambda: IMAGE, mc.IMAGE_MIMES, "tool_result", 0, 0, role="tool")
    tracer = tracer_provider.get_tracer("t")
    for config in (TraceConfig(hide_outputs=True), TraceConfig(hide_output_text=True),
                   TraceConfig(hide_output_messages=True), TraceConfig(hide_inputs=True),
                   TraceConfig(hide_input_images=True), TraceConfig(pii_redaction=True)):
        with tracer.start_as_current_span("tool") as span:
            assert mc.capture_parts(manager, span, "test", [part], direction="input", trace_configs=(config,),
                                    policy_directions=("input", "output")) == {}, config
    with tracer.start_as_current_span("tool") as span:
        captured = mc.capture_parts(manager, span, "test", [part], direction="input",
                                    trace_configs=(TraceConfig(),), policy_directions=("input", "output"))
    flush(manager)
    assert len(captured) == 1 and f01.payloads == [IMAGE]
    (body,) = f01.uploads
    assert body["direction"] == "input" and body["purpose"] == "tool_result"


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
