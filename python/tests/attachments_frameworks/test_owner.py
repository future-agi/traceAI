"""One model call, one owner: the core owner-mark helpers (no framework installed)."""

import asyncio

from conftest import b64, flush, png, refs
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager
from fi_instrumentation.attachments import media_capture as mc
from opentelemetry import context as otel_context

IMAGE = png(seed=21)
URI = "data:image/png;base64," + b64(IMAGE)


def _part():
    return mc.MediaPart("inline", lambda: URI, mc.IMAGE_MIMES, "model_input", 0, 0, role="user", raw=False)


def _capture(manager, span, namespace="fw"):
    return mc.capture_parts(manager, span, namespace, [_part()], direction="input")


def _under(value):
    return otel_context.attach(otel_context.set_value(mc.OWNER_KEY, value))


def test_call_owner_is_gated_on_an_enabled_manager_and_an_unowned_call(f01):
    assert mc.call_owner(None, "fw") is None
    assert mc.call_owner(create_attachment_manager(config=AttachmentConfig(enabled=False)), "fw") is None
    manager = f01.manager()
    owner = mc.call_owner(manager, "fw")
    assert isinstance(owner, mc.CallOwner) and owner.namespace == "fw"
    token = _under(True)  # an outer adapter or traceai.with_attachment_ownership
    try:
        assert mc.call_owner(manager, "fw") is None
    finally:
        otel_context.detach(token)


def test_owned_call_marks_only_the_enclosed_block():
    assert not mc.owned_elsewhere()
    with mc.owner_scope(mc.CallOwner("fw")):
        assert mc.owned_elsewhere()
    assert not mc.owned_elsewhere()
    try:
        with mc.owner_scope(mc.CallOwner("fw")):
            raise RuntimeError("provider failed")
    except RuntimeError:
        pass
    assert not mc.owned_elsewhere()
    with mc.owner_scope(None):
        assert not mc.owned_elsewhere()


def test_capture_parts_captures_inside_its_own_mark_only(f01, spans):
    tracer_provider, exporter = spans
    manager = f01.manager()
    tracer = tracer_provider.get_tracer("t")
    results = {}
    for label, mark, namespace in (("own", mc.CallOwner("fw"), "fw"), ("other", mc.CallOwner("other"), "fw"),
                                   ("anonymous", mc.CallOwner(None), "fw"), ("user", True, "fw")):
        with tracer.start_as_current_span(label) as span:
            token = _under(mark)
            try:
                results[label] = _capture(manager, span, namespace)
                # The adapter's capture restores the mark for the provider call that follows.
                assert otel_context.get_value(mc.OWNER_KEY) is mark
            finally:
                otel_context.detach(token)
    flush(manager)
    assert len(results["own"]) == 1
    assert results["other"] == {} and results["anonymous"] == {} and results["user"] == {}
    assert f01.payloads == [IMAGE]
    assert [s.name for s in exporter.get_finished_spans() if refs(s)] == ["own"]


def test_owned_stream_marks_each_advance_never_the_callers_code():
    seen = []

    def stream():
        seen.append(mc.owned_elsewhere())
        yield 1
        seen.append(mc.owned_elsewhere())
        yield 2

    owner = mc.CallOwner("fw")
    wrapped = mc.owned_stream(stream(), owner)
    caller = []
    for _ in wrapped:
        caller.append(mc.owned_elsewhere())
    assert seen == [True, True] and caller == [False, False]

    closed = []

    def abandoned():
        try:
            yield 1
            yield 2
        finally:
            closed.append(mc.owned_elsewhere())

    wrapped = mc.owned_stream(abandoned(), owner)
    next(wrapped)
    assert not mc.owned_elsewhere()  # suspended mid-call: the caller's context is unmarked
    wrapped.close()
    assert closed == [True] and not mc.owned_elsewhere()
    assert mc.owned_stream(stream(), None) is not None
    assert mc.owned_stream([1], owner) == [1]  # not a generator: returned as is


def test_owned_async_stream_marks_each_awaited_step_only():
    seen, caller = [], []

    async def stream():
        seen.append(mc.owned_elsewhere())
        yield 1
        seen.append(mc.owned_elsewhere())
        yield 2

    async def run():
        async for _ in mc.owned_stream(stream(), mc.CallOwner("fw")):
            caller.append(mc.owned_elsewhere())

    asyncio.run(run())
    assert seen == [True, True] and caller == [False, False]
