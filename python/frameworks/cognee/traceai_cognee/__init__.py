"""Export Cognee's native OpenTelemetry spans to Future AGI."""
import atexit
from collections import OrderedDict
from collections.abc import Mapping
from threading import Event, RLock, Thread
from weakref import WeakKeyDictionary
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import ProxyTracerProvider
from ._exporter import CogneeExporter

__version__ = '0.1.0'
_lock = RLock()
_slots = WeakKeyDictionary()
_configuring = False
# Spans that start but never end must not grow memory without bound. An evicted
# entry cannot protect against faulty reuse of its IDs and identical start time.
_MAX_OPEN_SPANS = 65536


class _Ambiguous:
    """Open spans sharing one identity, none of which can be attributed safely.

    Until every such span ends or the cap evicts the marker, a later span
    reusing this identity cannot be matched with an earlier one's end.
    """
    __slots__ = ('open',)

    def __init__(self, open_spans):
        self.open = open_spans


def _span_key(span):
    context = getattr(span, 'context', None)
    if context is None:
        return None
    # The start time keeps a span from an earlier configuration apart from a
    # later one that reuses its IDs (for example a faulty ID generator).
    return context.trace_id, context.span_id, getattr(span, 'start_time', None)


def _attached(provider, slot):
    # Read-only check of the SDK's processor list, used only after a provider
    # raised from add_span_processor, to tell whether it attached the slot anyway.
    active = getattr(provider, '_active_span_processor', None)
    return any(processor is slot for processor in getattr(active, '_span_processors', ()))


class _Generation:
    """Lease callbacks without holding a lock across downstream code.

    Closing detaches immediately; the last outstanding callback closes the
    delegate. In particular, close never waits for a callback that may itself
    be waiting for a traced worker.
    """
    def __init__(self, delegate):
        self._lock = RLock()
        self._delegate = delegate
        self._inflight = 0
        self._closed = False
        self._exiting = False
        self._shutdown_complete = Event()
        # Registered after the SDK provider's hook: drain this generation before
        # provider teardown, when Python's normal thread-join phase is already over.
        atexit.register(self._shutdown_at_exit)

    def call(self, method, *args):
        with self._lock:
            if self._closed:
                return True if method == 'force_flush' else None
            delegate = self._delegate
            self._inflight += 1
        try:
            return getattr(delegate, method)(*args)
        finally:
            with self._lock:
                self._inflight -= 1
                closing = self._closed and self._inflight == 0
                if closing:
                    self._delegate = None
            if closing:
                self._shutdown_delegate(delegate)

    def _shutdown_delegate(self, delegate):
        with self._lock:
            if not self._exiting:
                # BatchSpanProcessor can be exporting on its own worker even when
                # no callback is leased. Normal shutdown must not join that worker:
                # its exporter may be waiting for the caller of Handle.shutdown().
                Thread(target=self._finish_shutdown, args=(delegate,),
                       name='traceai-cognee-close', daemon=False).start()
                return
        self._finish_shutdown(delegate)

    def _finish_shutdown(self, delegate):
        try:
            delegate.shutdown()
        finally:
            atexit.unregister(self._shutdown_at_exit)
            self._shutdown_complete.set()

    def _shutdown_at_exit(self):
        with self._lock:
            self._exiting = True
        self.close()
        # An earlier exit callback may already have initiated asynchronous close.
        # Waiting here also covers an outstanding leased callback finishing it.
        self._shutdown_complete.wait()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            delegate = self._delegate if not self._inflight else None
            if delegate is not None:
                self._delegate = None
        if delegate is not None:
            self._shutdown_delegate(delegate)


class _OwnedProcessor(SpanProcessor):
    """One reusable provider-owned slot, independent of handle generations."""
    def __init__(self):
        self._lock = RLock()
        self._handle = None
        self._shutdown = False
        # Each open span stays with the generation active when it started, so a
        # span that ends after reconfiguration never reaches the new project.
        self._open = OrderedDict()

    def current(self):
        with self._lock:
            return self._handle

    def install(self, handle):
        with self._lock:
            if self._shutdown:
                raise RuntimeError('provider processor has shut down')
            if self._handle is not None:
                raise RuntimeError('provider already configured')
            self._handle = handle

    def detach(self, handle):
        generation = handle._generation
        with self._lock:
            if self._handle is handle:
                self._handle = None
            # The detached generation's open spans are never exported. Keep their
            # identities marked until they end, so a later configuration cannot
            # adopt one through a reused identity.
            for key in [key for key, owner in self._open.items() if owner is generation]:
                self._open[key] = _Ambiguous(1)
        generation.close()

    def on_start(self, span, parent_context=None):
        key = _span_key(span)
        with self._lock:
            handle = self._handle
            if handle is None or key is None:
                return
            entry = self._open.get(key)
            if isinstance(entry, _Ambiguous):
                entry.open += 1
                return
            if entry is not None:
                self._open[key] = _Ambiguous(2)
                return
            generation = handle._generation
            if len(self._open) >= _MAX_OPEN_SPANS:
                self._open.popitem(last=False)
            self._open[key] = generation
        generation.call('on_start', span, parent_context)

    def on_end(self, span):
        key = _span_key(span)
        with self._lock:
            entry = self._open.get(key) if key is not None else None
            if isinstance(entry, _Ambiguous):
                entry.open -= 1
                if entry.open <= 0:
                    del self._open[key]
                return
            if entry is not None:
                del self._open[key]
        if entry is not None:
            entry.call('on_end', span)

    def force_flush(self, timeout_millis=30000):
        handle = self.current()
        return True if handle is None else handle.force_flush(timeout_millis)

    def shutdown(self):
        with self._lock:
            self._shutdown = True
            handle, self._handle = self._handle, None
            self._open = OrderedDict()
        if handle is not None:
            handle._generation.close()


class Handle:
    def __init__(self, provider, processor, generation, signature):
        self.provider, self._processor = provider, processor
        self._generation, self._signature = generation, signature

    def force_flush(self, timeout_millis=30000):
        return self._generation.call('force_flush', timeout_millis)

    def shutdown(self):
        self._processor.detach(self)


def configure(*, project_name, capture_content=False, endpoint=None, headers=None,
              exporter=None, provider=None):
    """Attach one exporter; call before Cognee first creates native spans.

    An injected exporter is transferred to this handle. Shutdown affects only this
    adapter's processor, not the provider or its other processors. Shutdown may
    defer delegate closure until an already-running callback returns.
    """
    if not isinstance(project_name, str) or not project_name:
        raise ValueError('project_name must be a nonempty string')
    if not isinstance(capture_content, bool):
        raise TypeError('capture_content must be a bool')
    if provider is not None and not isinstance(provider, TracerProvider):
        raise TypeError('Cognee adapter requires an OpenTelemetry SDK TracerProvider')
    if exporter is not None and (endpoint is not None or headers is not None):
        raise ValueError('use exporter or endpoint/headers, not both')
    if exporter is not None and not all(callable(getattr(exporter, name, None))
                                        for name in ('export', 'force_flush', 'shutdown')):
        raise TypeError('exporter must implement export, force_flush and shutdown')
    if endpoint is not None and (not isinstance(endpoint, str) or not endpoint):
        raise ValueError('endpoint must be a nonempty string')
    if headers is not None and (not isinstance(headers, Mapping) or
                                not all(isinstance(k, str) and isinstance(v, str)
                                        for k, v in headers.items())):
        raise TypeError('headers must map strings to strings')
    signature = (project_name, capture_content, endpoint,
                 tuple(sorted((headers or {}).items())), id(exporter) if exporter is not None else None)
    global _configuring
    with _lock:
        if _configuring:
            raise RuntimeError('another Cognee configure is in progress')
        _configuring = True
    generation = None
    try:
        global_provider = trace.get_tracer_provider()
        if provider is None:
            provider = TracerProvider() if isinstance(global_provider, ProxyTracerProvider) else global_provider
        elif not isinstance(global_provider, ProxyTracerProvider) and global_provider is not provider:
            raise ValueError('provider must be the global provider used by Cognee')
        if not isinstance(provider, TracerProvider):
            raise TypeError('Cognee adapter requires an OpenTelemetry SDK TracerProvider')
        if getattr(provider, '_default_processor', False):
            raise RuntimeError('provider has a replaceable default processor; use an ordinary OTel provider')
        with _lock:
            slot = _slots.get(provider)
        existing = slot.current() if slot else None
        if existing is not None:
            if existing._signature != signature:
                raise ValueError('provider already configured with different options')
            return existing
        if exporter is None:
            from fi_instrumentation.otel import HTTPSpanExporter
            exporter = HTTPSpanExporter(endpoint=endpoint, headers=headers)
        generation = _Generation(BatchSpanProcessor(CogneeExporter(exporter, project_name, capture_content)))
        new_slot = slot is None
        if new_slot:
            slot = _OwnedProcessor()
        handle = Handle(provider, slot, generation, signature)
        slot.install(handle)
        if new_slot:
            with _lock:
                _slots[provider] = slot
            try:
                provider.add_span_processor(slot)
            except BaseException:
                # A provider may attach the slot and then raise. Keep an attached
                # slot registered so a retry reuses it instead of adding another.
                if not _attached(provider, slot):
                    with _lock:
                        if _slots.get(provider) is slot:
                            del _slots[provider]
                raise
        if isinstance(global_provider, ProxyTracerProvider):
            trace.set_tracer_provider(provider)
            if trace.get_tracer_provider() is not provider:
                raise RuntimeError('could not install global tracer provider')
        generation = None
        return handle
    except BaseException:
        if generation is not None:
            if 'handle' in locals():
                slot.detach(handle)
            else:
                generation.close()
        raise
    finally:
        with _lock:
            _configuring = False

__all__ = ['configure', 'Handle', '__version__']
