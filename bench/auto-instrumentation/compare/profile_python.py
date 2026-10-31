"""Profiling-only runner. No agent sources are changed; runtime timers are optional."""
import cProfile
import functools
import gc
import importlib
import json
import os
import pstats
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path


def configure_processor():
    if os.environ['PROFILE_TOOL'] != 'traceai':
        return 'native'
    from fi_instrumentation.auto import _manager
    from fi_instrumentation.otel import SimpleSpanProcessor, BatchSpanProcessor
    policy = os.environ['PROFILE_PROCESSOR']
    overlay = next(iter(_manager._state.overlays.values()))
    old = overlay._delegate
    exporter = getattr(old, 'span_exporter', None)
    if exporter is None:
        exporter = old._batch_processor._exporter
    old.force_flush()
    # Retain the exact privacy exporter/overlay, sampler and wire hooks. Do not
    # shutdown the old delegate: shutdown would close the shared exporter.
    overlay._delegate = (SimpleSpanProcessor(span_exporter=exporter) if policy == 'simple'
                         else BatchSpanProcessor(span_exporter=exporter, schedule_delay_millis=200,
                                                 max_queue_size=2048, max_export_batch_size=512))
    return policy


class Timers:
    """Exclusive, nested *thread CPU* timers; not added latency or p50 attribution."""
    def __init__(self):
        self.owner = threading.get_ident()
        self.stack = []
        self.ns = defaultdict(int)
        self.calls = defaultdict(int)
        self.boundary_calls = defaultdict(int)
        self.originals = []
        self.gc_start = None

    def patch(self, obj, name, stage):
        old = getattr(obj, name, None)
        if not callable(old):
            return
        @functools.wraps(old)
        def timed(*args, **kwargs):
            if threading.get_ident() != self.owner:
                return old(*args, **kwargs)
            entry = [stage, time.thread_time_ns(), 0]
            self.stack.append(entry)
            try:
                return old(*args, **kwargs)
            finally:
                elapsed = time.thread_time_ns() - entry[1]
                self.stack.pop()
                self.ns[stage] += max(0, elapsed - entry[2])
                self.calls[stage] += 1
                self.boundary_calls[old.__module__ + '.' + old.__qualname__] += 1
                if self.stack:
                    self.stack[-1][2] += elapsed
        try:
            setattr(obj, name, timed)
            self.originals.append((obj, name, old))
        except (TypeError, AttributeError):
            pass

    def gc_callback(self, phase, info):
        if threading.get_ident() != self.owner:
            return
        if phase == 'start':
            self.gc_start = time.thread_time_ns()
        elif self.gc_start is not None:
            elapsed = time.thread_time_ns() - self.gc_start
            self.ns['gc'] += elapsed
            if self.stack:
                self.stack[-1][2] += elapsed
            self.gc_start = None

    def install(self, tool):
        if tool == 'traceai':
            from fi_instrumentation.auto.wire import _core, _capture, _privacy
            for name in ('match', 'resolve_provider', 'check_request', 'build'):
                self.patch(_core, name, 'attributes')
            for name in ('parse_sse', 'parse_ndjson', '_resp_openai_chat'):
                self.patch(_core, name, 'sse_reassembly' if name != '_resp_openai_chat' else 'attributes')
            self.patch(_capture.Recorder, 'begin', 'wrapper')
            self.patch(_capture.Recorder, 'observe_credentials', 'privacy_S1')
            for name in ('on_headers', 'on_chunk', 'finish'):
                self.patch(_capture.Capture, name, 'transport_buffer')
            self.patch(_capture.Capture, '_emit', 'span')
            # Recursive scrub is covered by its outer boundary; wrapping each
            # recursive node would primarily measure timer overhead.
            for name in ('observe_request', 'observe_body', 'secrets'):
                self.patch(_privacy, name, 'privacy_S1')
            for name in ('mask_attributes', 'export_span', 'protect_live_span'):
                self.patch(_privacy, name, 'privacy_S5_S6')
            import fi_instrumentation.instrumentation._tracers as tracers
            for cls in ('FITracer', 'FiTracer', '_FiTracer'):
                if hasattr(tracers, cls):
                    self.patch(getattr(tracers, cls), 'start_span', 'span')
        if tool != 'baseline':
            from opentelemetry import context
            for name in ('get_current', 'get_value', 'set_value', 'attach', 'detach'):
                self.patch(context, name, 'context')
            from opentelemetry.sdk.trace import Tracer, Span
            self.patch(Tracer, 'start_span', 'span')
            self.patch(Span, 'end', 'processor_export')
            self.patch(Span, 'set_attributes', 'attributes')
            if tool == 'braintrust':
                import braintrust.logger as logger
                for clsname in ('SpanImpl', 'Logger'):
                    cls = getattr(logger, clsname, None)
                    if cls:
                        for name in ('start_span', '__init__', 'log', 'end'):
                            self.patch(cls, name, 'wrapper' if name == 'log' else 'span')
                for name in ('deep_copy_event', 'merge_row_batch', 'sanitize_partial_args'):
                    self.patch(logger, name, 'attributes')
        # Common codec/copy timings are WHOLE-APPLICATION, not instrumentation
        # overhead. Keep a separate row so no SDK cost is credited to an agent.
        for name in ('loads', 'dumps'):
            self.patch(json, name, 'json_codec_all')
        gc.callbacks.append(self.gc_callback)

    def close(self):
        gc.callbacks.remove(self.gc_callback)
        for obj, name, old in reversed(self.originals):
            setattr(obj, name, old)


def main():
    source = Path(os.environ['PROFILE_APP']).read_text()
    namespace = {'__name__': '__profile_application__', '__file__': os.environ['PROFILE_APP']}
    exec(compile(source.split('print(json.dumps({"kind": "ready"}')[0], os.environ['PROFILE_APP'], 'exec'), namespace)
    processor = configure_processor()
    invoke = namespace['invoke']
    for _ in range(int(os.environ['BENCH_WARMUP'])):
        invoke('compare-input-canary')
    mode = os.environ['PROFILE_MODE']
    n = int(os.environ['BENCH_N'])
    timer = Timers()
    # The process clock is monotonic across background callbacks. A thread CPU
    # clock used by cProfile can jump backwards when Python 3.12 monitoring sees
    # another thread, producing impossible negative self times. This Python
    # callback is expensive PER EVENT and biases high-call-count stages: retain
    # this raw diagnostic, but never rescale its shares to unprofiled CPU/latency.
    profiler = cProfile.Profile(timer=time.process_time_ns, timeunit=1e-9)
    if mode == 'internal':
        timer.install(os.environ['PROFILE_TOOL'])
    print(json.dumps({'kind': 'ready'}), flush=True)
    if mode == 'cpu':
        profiler.enable()
    total_cpu = time.thread_time_ns()
    calls = []
    for i in range(n):
        start = time.perf_counter_ns()
        cpu = time.thread_time_ns()
        response, _, _ = invoke('compare-input-canary')
        calls.append({'kind': 'call', 'i': i, 'latency_us': (time.perf_counter_ns() - start) / 1000,
                      'cpu_us': (time.thread_time_ns() - cpu) / 1000, 'response': response})
    total_cpu = (time.thread_time_ns() - total_cpu) / 1000
    if mode == 'cpu':
        profiler.disable()
        profiler.dump_stats(os.environ['PROFILE_RAW'])
    if mode == 'internal':
        timer.close()
    for call in calls:
        print(json.dumps(call, ensure_ascii=False, default=str), flush=True)
    functions = []
    if mode == 'cpu':
        for (file, line, name), (cc, nc, tt, ct, callers) in pstats.Stats(profiler).stats.items():
            functions.append({'file': file, 'line': line, 'function': name, 'self_us': tt * 1e6 / n,
                              'inclusive_us': ct * 1e6 / n, 'calls_per_call': nc / n})
    print(json.dumps({'kind': 'profile', 'processor': processor, 'cpu_us_per_call': total_cpu / n,
                      'stages_us': {k: v / 1000 / n for k, v in timer.ns.items()},
                      'stage_calls_per_call': {k: v / n for k, v in timer.calls.items()},
                      'boundary_calls_per_call': {k: v / n for k, v in timer.boundary_calls.items()},
                      'functions': sorted(functions, key=lambda f: -f['self_us'])}), flush=True)
    time.sleep(float(os.environ['BENCH_SETTLE']))
    if os.environ['PROFILE_TOOL'] == 'braintrust':
        namespace['compare_logger'].flush()
    elif os.environ['PROFILE_TOOL'] == 'traceai':
        from fi_instrumentation.auto import flush
        flush()
    print(json.dumps({'kind': 'done'}), flush=True)


if __name__ == '__main__':
    main()
