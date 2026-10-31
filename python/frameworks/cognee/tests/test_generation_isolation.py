"""Spans stay with the configuration active when they started; all inputs are synthetic."""
import subprocess
import sys
import unittest

ISOLATION = '''
import threading
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import traceai_cognee
from traceai_cognee import configure
LLM = 'cognee.observe.acreate_structured_output'
INPUT = 'langfuse.observation.input'
a, b = InMemorySpanExporter(), InMemorySpanExporter()
ha = configure(project_name='project-A', exporter=a, capture_content=True)
tracer = ha.provider.get_tracer('test')

# Ends while A is active: exported by A.
with tracer.start_as_current_span(LLM, attributes={INPUT: 'kept-by-A'}): pass
# Starts under A, ends after A shuts down and B is configured: exported by neither.
late = tracer.start_span(LLM, attributes={INPUT: 'data-owned-by-A'})
assert ha.force_flush()
ha.shutdown()
# Starts while nothing is configured: a later configuration does not adopt it.
orphan = tracer.start_span(LLM, attributes={INPUT: 'data-owned-by-nobody'})
hb = configure(project_name='project-B', exporter=b, capture_content=True)
late.end(); orphan.end()
with tracer.start_as_current_span(LLM, attributes={INPUT: 'owned-by-B'}): pass
assert hb.force_flush()
seen_a = [s.attributes.get('input.value') for s in a.get_finished_spans()]
seen_b = [(s.resource.attributes['project_name'], s.attributes.get('input.value')) for s in b.get_finished_spans()]
assert seen_a == ['kept-by-A'], seen_a
assert seen_b == [('project-B', 'owned-by-B')], seen_b

# Interleaved spans across concurrent threads under B are all exported once, by B.
def work(i):
    with tracer.start_as_current_span('cognee.search.query', attributes={'gen_ai.usage.input_tokens': i}): pass
threads = [threading.Thread(target=work, args=(i,)) for i in range(64)]
[t.start() for t in threads]; [t.join(10) for t in threads]
assert hb.force_flush()
tokens = sorted(s.attributes['gen_ai.usage.input_tokens'] for s in b.get_finished_spans()[1:])
assert tokens == list(range(64)), tokens
slot = traceai_cognee._slots[hb.provider]
assert not slot._open, len(slot._open)
hb.shutdown()
'''

CAP = '''
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import traceai_cognee
from traceai_cognee import configure
traceai_cognee._MAX_OPEN_SPANS = 3
sink = InMemorySpanExporter()
h = configure(project_name='cap', exporter=sink)
tracer = h.provider.get_tracer('test')
spans = [tracer.start_span('open' + str(i)) for i in range(5)]
slot = traceai_cognee._slots[h.provider]
assert len(slot._open) == 3, len(slot._open)
spans[0].end()  # evicted while open: dropped, never re-attributed
spans[4].end()  # still tracked: exported
assert h.force_flush()
assert [s.name for s in sink.get_finished_spans()] == ['open4'], sink.get_finished_spans()
h.shutdown()
# Spans still open at shutdown stay marked, unexported, until they end.
assert all(isinstance(v, traceai_cognee._Ambiguous) for v in slot._open.values()), slot._open
spans[2].end(); spans[3].end()
assert not slot._open
'''

DUPLICATE_IDS = '''
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_cognee import configure
LLM = 'cognee.observe.acreate_structured_output'
INPUT = 'langfuse.observation.input'
class Fixed(IdGenerator):
    def generate_span_id(self): return 2
    def generate_trace_id(self): return 1
p = TracerProvider(id_generator=Fixed(), shutdown_on_exit=False)
a, b = InMemorySpanExporter(), InMemorySpanExporter()
ha = configure(project_name='A', provider=p, exporter=a, capture_content=True)
tracer = p.get_tracer('test')
old = tracer.start_span(LLM, attributes={INPUT: 'data-from-A'})
ha.shutdown()
hb = configure(project_name='B', provider=p, exporter=b, capture_content=True)
new = tracer.start_span(LLM, attributes={INPUT: 'data-from-B'})
# The reused IDs must not let A's span take B's place.
old.end(); new.end()
assert hb.force_flush()
seen = [s.attributes.get('input.value') for s in b.get_finished_spans()]
assert seen == ['data-from-B'], seen
# Two open spans with the same identity cannot be told apart: neither is exported.
twins = [tracer.start_span('twin', start_time=10**18) for _ in range(2)]
twins[0].end(); twins[1].end()
assert hb.force_flush()
assert [s.name for s in b.get_finished_spans()] == [LLM], b.get_finished_spans()
hb.shutdown()
'''

IDENTICAL_IDENTITY = '''
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import traceai_cognee
from traceai_cognee import configure
INPUT = 'langfuse.observation.input'
class Fixed(IdGenerator):
    def generate_span_id(self): return 2
    def generate_trace_id(self): return 1
p = TracerProvider(id_generator=Fixed(), shutdown_on_exit=False)
a, b = InMemorySpanExporter(), InMemorySpanExporter()
ha = configure(project_name='A', provider=p, exporter=a, capture_content=True)
tracer = p.get_tracer('test')
# Same IDs and start time across a reconfiguration: B must not adopt A's span.
old = tracer.start_span('old', start_time=10**18, attributes={INPUT: 'data-from-A'})
ha.shutdown()
hb = configure(project_name='B', provider=p, exporter=b, capture_content=True)
new = tracer.start_span('new', start_time=10**18)
old.end(); new.end()
assert hb.force_flush()
assert not b.get_finished_spans(), [(s.name, s.attributes.get('input.value')) for s in b.get_finished_spans()]
# Overlapping duplicates stay unattributable until every one has ended.
first = tracer.start_span('first', start_time=2 * 10**18)
second = tracer.start_span('second', start_time=2 * 10**18)
first.end()
third = tracer.start_span('third', start_time=2 * 10**18)
second.end(); third.end()
fresh = tracer.start_span('fresh', start_time=2 * 10**18); fresh.end()
assert hb.force_flush()
assert [s.name for s in b.get_finished_spans()] == ['fresh'], [s.name for s in b.get_finished_spans()]
assert not traceai_cognee._slots[p]._open
hb.shutdown()
'''

PARTIAL_ATTACH = '''
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_cognee import configure
class AttachThenRaise(TracerProvider):
    def add_span_processor(self, processor):
        super().add_span_processor(processor)
        raise RuntimeError('provider failed after attaching')
class RaiseBeforeAttach(TracerProvider):
    failures = 2
    def add_span_processor(self, processor):
        if RaiseBeforeAttach.failures:
            RaiseBeforeAttach.failures -= 1
            raise RuntimeError('provider failed before attaching')
        return super().add_span_processor(processor)
# Each case runs in its own process: the first configure sets the global provider.
cls, retries = {'attach_then_raise': (AttachThenRaise, 1), 'raise_before_attach': (RaiseBeforeAttach, 2)}[CASE]
provider = cls(shutdown_on_exit=False)
# A handle owns and closes its exporter, so each configuration gets its own.
for _ in range(retries):
    try:
        configure(project_name='p', provider=provider, exporter=InMemorySpanExporter())
        raise AssertionError('provider error was swallowed')
    except RuntimeError as error:
        assert 'provider failed' in str(error), error
handle = configure(project_name='p', provider=provider, exporter=InMemorySpanExporter())
for _ in range(3):
    handle.shutdown()
    sink = InMemorySpanExporter()
    handle = configure(project_name='p', provider=provider, exporter=sink)
assert len(provider._active_span_processor._span_processors) == 1, cls
with provider.get_tracer('t').start_as_current_span('after-retry'): pass
assert handle.force_flush()
assert [s.name for s in sink.get_finished_spans()] == ['after-retry'], cls
handle.shutdown()
'''


class GenerationIsolationTests(unittest.TestCase):
    def run_fresh(self, code, case=None):
        if case is not None:
            code = 'CASE = %r\n' % case + code
        result = subprocess.run([sys.executable, '-c', code], cwd=sys.prefix,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_spans_never_cross_configurations(self):
        self.run_fresh(ISOLATION)

    def test_open_span_tracking_is_bounded(self):
        self.run_fresh(CAP)

    def test_duplicate_span_ids_never_cross_configurations(self):
        self.run_fresh(DUPLICATE_IDS)

    def test_identical_identity_is_never_adopted_or_reattributed(self):
        self.run_fresh(IDENTICAL_IDENTITY)

    def test_attach_then_raise_retry_reuses_one_slot(self):
        self.run_fresh(PARTIAL_ATTACH, 'attach_then_raise')

    def test_raise_before_attach_retry_reuses_one_slot(self):
        self.run_fresh(PARTIAL_ATTACH, 'raise_before_attach')


if __name__ == '__main__':
    unittest.main()
