"""T-CS: consumed single-object streams (design section 6)."""

import gc
import json
import threading
import time

from fi_instrumentation.attachments import StreamOptions
from opentelemetry.sdk.trace import TracerProvider

from test_policy_runtime import CONTEXT


def options(max_bytes=100, **kw):
    return StreamOptions("audio/mpeg", "output", max_bytes, purpose="model_output", **kw)


class AppStream:
    """An SDK-owned iterator the wrapper already owns; the core only sees consumed chunks."""

    def __init__(self, chunks):
        self.chunks, self.reads = list(chunks), 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


def observe(manager, source, target=CONTEXT, **kw):
    stream = manager.start_attachment_stream(target, options(**kw))
    for chunk in source:
        stream.try_write(chunk)
        yield chunk
    stream.finish()


def test_wrapped_iterator_observes_only_consumed(manager, transport):
    """T-CS-07 (Py analogue of T-CS-01): no extra reads beyond what the app consumed."""
    app = AppStream([b"ab", b"cd", b"ef"])
    consumed = list(observe(manager, app))
    assert consumed == [b"ab", b"cd", b"ef"] and app.reads == 3
    manager.flush(2)
    assert transport.payloads == [b"abcdef"]
    assert transport.requests[0]["declared_bytes"] == 6 and transport.requests[0]["purpose"] == "model_output"


def test_over_cap_discards_everything(manager, transport):
    """T-CS-02."""
    stream = manager.start_attachment_stream(CONTEXT, options(max_bytes=4))
    assert stream.try_write(b"abc")
    assert stream.try_write(b"de") is False and stream.reason == "too_large"
    assert stream.try_write(b"f") is False
    assert stream.finish().admission == "too_large"
    assert manager.owned_bytes == 0 and not transport.requests


def test_app_cancel_releases_budget(manager, transport):
    """T-CS-03."""
    stream = manager.start_attachment_stream(CONTEXT, options())
    stream.try_write(b"abc")
    stream.abort()
    assert stream.reason == "incomplete" and manager.owned_bytes == 0
    assert stream.finish().admission == "incomplete" and not transport.requests


def test_abandoned_stream_aborts_lazily(manager, monkeypatch):
    """T-CS-04: no timer; aborted at the next entry point after idle_ttl."""
    timers = []
    monkeypatch.setattr(threading, "Timer", lambda *a, **kw: timers.append(a))
    stream = manager.start_attachment_stream(CONTEXT, options(idle_ttl=0.05))
    stream.try_write(b"x")
    time.sleep(0.1)
    status = manager.status()
    assert status.open_streams == 0 and status.owned_bytes == 0
    assert stream.reason == "incomplete" and not timers


def test_gc_returns_budget(manager):
    """T-CS-05."""
    stream = manager.start_attachment_stream(CONTEXT, options())
    stream.try_write(b"abc")
    assert manager.owned_bytes == 100
    del stream
    gc.collect()
    assert manager.owned_bytes == 0


def test_byob_buffer_reuse(manager, transport):
    """T-CS-06."""
    buffer = bytearray(b"1234")
    stream = manager.start_attachment_stream(CONTEXT, options())
    stream.try_write(memoryview(buffer))
    buffer[:] = b"zzzz"
    stream.try_write(memoryview(buffer))
    stream.finish().result(2)
    assert transport.payloads == [b"1234zzzz"]


def test_reference_projected_only_at_finish(manager, transport):
    """T-CH-05 (streams): over-cap or aborted streams project nothing."""
    provider = TracerProvider(shutdown_on_exit=False)
    span = provider.get_tracer("t").start_span("s")
    aborted = manager.start_attachment_stream(span, options(max_bytes=2))
    aborted.try_write(b"abc")
    aborted.finish()
    assert "fi.attachment.refs" not in (span.attributes or {})
    stream = manager.start_attachment_stream(span, options(role="assistant", channel=1))
    stream.try_write(b"abc")
    assert "fi.attachment.refs" not in (span.attributes or {})
    handle = stream.finish()
    refs = json.loads(span.attributes["fi.attachment.refs"])
    assert refs == [{"attachment_id": handle.attachment_id, "occurrence_id": handle.occurrence_id,
                     "direction": "output", "modality": "audio", "mime_type": "audio/mpeg",
                     "purpose": "model_output", "role": "assistant", "channel": 1}]
    assert "role" not in transport.requests[-1] if transport.requests else True
    span.end()
    provider.shutdown()


def test_zero_length_is_incomplete(manager, transport):
    stream = manager.start_attachment_stream(CONTEXT, options())
    assert stream.finish().admission == "incomplete" and manager.owned_bytes == 0
