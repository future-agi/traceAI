"""Bounded lifecycle regressions; all exporters and inputs are synthetic."""
import gc
import subprocess
import sys
import threading
import unittest
import weakref

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from traceai_cognee import configure


class Sink(SpanExporter):
    def __init__(self):
        self.names = []
        self.closed = 0
        self.closed_event = threading.Event()
    def export(self, spans):
        self.names.extend(s.name for s in spans)
        return SpanExportResult.SUCCESS
    def shutdown(self):
        self.closed += 1
        self.closed_event.set()


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        existing = trace.get_tracer_provider()
        if isinstance(existing, TracerProvider):
            cls.provider = existing
        else:
            cls.provider = TracerProvider(shutdown_on_exit=False)
            trace.set_tracer_provider(cls.provider)

    def test_reentry_and_close_during_flush(self):
        started, release, worker_done = (threading.Event() for _ in range(3))
        class Reentrant(Sink):
            def export(s, spans):
                if not started.is_set():
                    started.set()
                    def work():
                        with tracer.start_as_current_span('worker'):
                            pass
                        worker_done.set()
                    t = threading.Thread(target=work, daemon=True)
                    t.start()
                    if not worker_done.wait(2):
                        raise AssertionError('traced exporter worker blocked')
                    if not release.wait(2):
                        raise AssertionError('test did not release exporter')
                return super().export(spans)
        sink = Reentrant()
        handle = configure(project_name='reentry', exporter=sink)
        tracer = self.provider.get_tracer('test')
        with tracer.start_as_current_span('original'): pass
        flush = threading.Thread(target=handle.force_flush, daemon=True)
        flush.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertTrue(worker_done.wait(3))
            close = threading.Thread(target=handle.shutdown, daemon=True)
            close.start(); close.join(1)
            self.assertFalse(close.is_alive(), 'shutdown waited for exporter')
        finally:
            release.set(); flush.join(5)
        self.assertFalse(flush.is_alive())
        self.assertTrue(sink.closed_event.wait(5))
        self.assertEqual(sink.closed, 1)
        handle.shutdown()
        self.assertEqual(sink.closed, 1)

    def test_reuse_old_handles_collection_and_provider_shutdown(self):
        baseline = len(self.provider._active_span_processor._span_processors)
        refs = []
        old = None
        for index in range(25):
            sink = Sink(); refs.append(weakref.ref(sink))
            h = configure(project_name='p' + str(index), exporter=sink)
            if old:
                old.shutdown()
                self.assertTrue(old.force_flush())
            with self.provider.get_tracer('test').start_as_current_span('span' + str(index)): pass
            self.assertTrue(h.force_flush())
            self.assertEqual(sink.names, ['span' + str(index)])
            h.shutdown(); self.assertTrue(sink.closed_event.wait(5)); self.assertEqual(sink.closed, 1)
            old = h
            del sink, h
        gc.collect()
        self.assertTrue(all(ref() is None for ref in refs))
        self.assertLessEqual(len(self.provider._active_span_processor._span_processors), baseline + 1)

    def test_raw_refusal_and_validation_leave_global_usable(self):
        code = '''
from opentelemetry import trace
from opentelemetry.trace import ProxyTracerProvider
from opentelemetry.sdk.trace import TracerProvider as Ordinary
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from fi_instrumentation.otel import TracerProvider
from traceai_cognee import configure
before = trace.get_tracer_provider()
assert isinstance(before, ProxyTracerProvider)
p = TracerProvider(verbose=False)
old = p._active_span_processor._span_processors
sink = InMemorySpanExporter()
try:
    configure(project_name='x', provider=p, exporter=sink)
    raise AssertionError('raw provider accepted')
except RuntimeError:
    pass
assert trace.get_tracer_provider() is before
assert p._active_span_processor._span_processors == old and p._default_processor
try:
    configure(project_name='x', headers=object())
    raise AssertionError('invalid headers accepted')
except (TypeError, AttributeError):
    pass
assert trace.get_tracer_provider() is before
try:
    configure(project_name='x', exporter=object())
    raise AssertionError('invalid exporter accepted')
except TypeError:
    pass
assert trace.get_tracer_provider() is before
h = configure(project_name='valid', provider=Ordinary(shutdown_on_exit=False), exporter=sink)
assert trace.get_tracer_provider() is h.provider
h.shutdown(); p.shutdown()
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=sys.prefix, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_background_exporter_can_join_shutdown_worker(self):
        code = '''
import threading
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from traceai_cognee import configure
ready=threading.Event(); entered=threading.Event(); finished=threading.Event(); blocked=[]
class Sink(SpanExporter):
    def export(self, spans):
        entered.set()
        assert ready.wait(5)
        t=threading.Thread(target=h.shutdown, daemon=True); t.start(); t.join(1)
        blocked.append(t.is_alive())
        finished.set()
        return SpanExportResult.SUCCESS
    def shutdown(self): pass
h=configure(project_name='x', exporter=Sink())
tracer=h.provider.get_tracer('test')
for i in range(600):
    with tracer.start_as_current_span('span'): pass
assert entered.wait(5)
ready.set()
assert finished.wait(5)
assert not any(blocked), 'background exporter waited on shutdown worker, which waited on exporter'
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=sys.prefix, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overlapping_configure_fails_fast_and_first_succeeds(self):
        code = '''
import threading
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_cognee import configure
entered=threading.Event(); release=threading.Event(); result=[]
class Provider(TracerProvider):
    def add_span_processor(self, processor):
        entered.set()
        assert release.wait(3)
        return super().add_span_processor(processor)
p=Provider(shutdown_on_exit=False); sink=InMemorySpanExporter()
def first():
    result.append(configure(project_name='x', provider=p, exporter=sink))
t=threading.Thread(target=first, daemon=True); t.start(); assert entered.wait(3)
try:
    configure(project_name='x', provider=p, exporter=sink)
    raise AssertionError('overlapping configure accepted')
except RuntimeError as e:
    assert 'configure is in progress' in str(e)
finally:
    release.set()
t.join(5); assert not t.is_alive() and len(result)==1
assert configure(project_name='x', provider=p, exporter=sink) is result[0]
result[0].shutdown(); p.shutdown()
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=sys.prefix, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_provider_shutdown_closes_once_and_rejects_reuse(self):
        code = '''
from traceai_cognee import configure
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
s=InMemorySpanExporter(); h=configure(project_name='x', exporter=s)
h.provider.shutdown(); h.shutdown()
try:
    configure(project_name='again', exporter=InMemorySpanExporter())
    raise AssertionError('shutdown provider accepted')
except RuntimeError:
    pass
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=sys.prefix, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == '__main__':
    unittest.main()
