import threading
from traceai_anthropic._stream import _Stream


class Native:
    """A thread-safe native iterator, so two threads can advance one stream."""
    def __init__(self, items): self.items = iter(items); self.lock = threading.Lock()
    def __next__(self):
        with self.lock: return next(self.items)
    def close(self): return None


def lock_free(stream, seen):
    # Record whether another thread can take the lifecycle lock right now.
    def probe():
        got = stream._self_lock.acquire(timeout=0.5); seen.append(got)
        if got: stream._self_lock.release()
    t = threading.Thread(target=probe); t.start(); t.join(2)


def test_concurrent_advances_merge_in_arrival_order():
    ready, release = threading.Event(), threading.Event()
    class Slow:
        def model_dump(self, **kwargs):
            ready.set(); release.wait(3); return {'completion': 'A'}
    class Fast:
        def model_dump(self, **kwargs): return {'completion': 'B'}
    s = _Stream(Native([Slow(), Fast()]), None)
    first = threading.Thread(target=lambda: next(s)); first.start(); assert ready.wait(2)
    second = threading.Thread(target=lambda: next(s)); second.start(); second.join(2)
    assert not second.is_alive()  # the later item finished extracting first
    release.set(); first.join(3)
    assert s._response_accumulator._result() == {'completion': 'AB'}
    assert not s._self_pending and not s._self_extracting


def test_serializer_result_runs_no_foreign_code_under_lock():
    seen = []; holder = {}
    class Foreign:
        def __deepcopy__(self, memo): lock_free(holder['s'], seen); return 'copied'
        def __str__(self): lock_free(holder['s'], seen); return 'foreign'
    class Result(dict):
        def get(self, *a): lock_free(holder['s'], seen); return super().get(*a)
        def items(self): lock_free(holder['s'], seen); return super().items()
        def keys(self): lock_free(holder['s'], seen); return super().keys()
        def __iter__(self): lock_free(holder['s'], seen); return super().__iter__()
        def __getitem__(self, k): lock_free(holder['s'], seen); return super().__getitem__(k)
    class Text(str):
        def __add__(self, other): lock_free(holder['s'], seen); return str(self) + other
    class Item:
        def model_dump(self, **kwargs): return Result(completion=Text('hello'), extra=Foreign())
    s = _Stream(Native([Item()]), None); holder['s'] = s
    next(s)
    assert seen and all(seen), seen
    result = s._response_accumulator._result()
    assert result['completion'] == 'hello' and type(result['completion']) is str
    assert result['extra'] == 'foreign'
