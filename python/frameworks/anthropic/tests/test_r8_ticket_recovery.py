import threading
import pytest
from traceai_anthropic import _stream
from traceai_anthropic._stream import _Stream


class Native:
    """A thread-safe native iterator, so two threads can advance one stream."""
    def __init__(self, items): self.items = iter(items); self.lock = threading.Lock()
    def __next__(self):
        with self.lock: return next(self.items)
    def close(self): return None


class Item:
    def __init__(self, text='x', ready=None, release=None): self.text = text; self.ready = ready; self.release = release
    def model_dump(self, **kwargs):
        if self.ready:
            self.ready.set(); self.release.wait(5)
        return {'completion': self.text}


MAX_PENDING = getattr(_stream, '_MAX_PENDING', 1024)


def test_interrupt_during_merge_leaves_nothing_stranded(monkeypatch):
    s = _Stream(Native([Item('a')] + [Item() for _ in range(1000)]), None)
    merge = _stream._ResponseAccumulator.merge
    calls = []
    def interrupted(self, values):
        calls.append(values)
        if len(calls) == 1:
            raise KeyboardInterrupt('async interrupt while merging')
        return merge(self, values)
    monkeypatch.setattr(_stream._ResponseAccumulator, 'merge', interrupted)
    with pytest.raises(KeyboardInterrupt):
        next(s)
    for _ in range(1000):
        next(s)
    assert not s._self_pending and not getattr(s, '_self_extracting', ()) and s._self_active == 0
    assert s._response_accumulator._result() == {'completion': 'x' * 1000}


def test_stalled_extraction_keeps_the_queue_bounded():
    ready, release = threading.Event(), threading.Event()
    s = _Stream(Native([Item('slow', ready, release)] + [Item() for _ in range(2 * MAX_PENDING)]), None)
    slow = threading.Thread(target=lambda: next(s)); slow.start(); assert ready.wait(2)
    for _ in range(2 * MAX_PENDING):
        next(s)
        assert len(s._self_pending) <= MAX_PENDING + 1
    release.set(); slow.join(5); assert not slow.is_alive()
    assert not s._self_pending and not getattr(s, '_self_extracting', ()) and not getattr(s, '_self_skipped', ())
    # The stalled item lost its place, so its telemetry is dropped; every later item is kept.
    assert s._response_accumulator._result() == {'completion': 'x' * (2 * MAX_PENDING)}
