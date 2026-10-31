"""One reusable processor per global provider, with bounded cleanup waits."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping
from threading import Event, RLock, Thread
from weakref import WeakKeyDictionary

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import ProxyTracerProvider

from ._projection import is_native, project

_logger = logging.getLogger(__name__)
_lock = RLock()
_slots: WeakKeyDictionary[TracerProvider, _Slot] = WeakKeyDictionary()
_configuring = False
_MAX_OPEN = 65536


def _timeout(value):
    if type(value) is not int or value < 0 or value > 300000:
        raise ValueError("timeout_millis must be an integer between 0 and 300000")
    return value / 1000


def _key(span):
    context = span.context
    return context.trace_id, context.span_id, span.start_time


class _Pipeline:
    def __init__(self, exporter, signature, project_name, capture_content):
        self.signature = signature
        self.project_name, self.capture_content = project_name, capture_content
        # Only projected copies enter this queue. SDK defaults overridden to bound memory.
        self.processor = BatchSpanProcessor(
            exporter,
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
            export_timeout_millis=10000,
        )
        self.lock = RLock()
        self.closed = False
        self.done = Event()
        self.close_ok = False
        self.flush_done = Event()
        self.flush_done.set()
        self.flush_ok = True

    def force_flush(self, timeout_millis):
        seconds = _timeout(timeout_millis)
        with self.lock:
            if self.closed:
                return self.done.is_set() and self.close_ok
            if self.flush_done.is_set():
                self.flush_ok = False
                self.flush_done.clear()
                # Some SDK versions/exporters ignore flush deadlines. One worker at a
                # time limits callers' wait without accumulating blocked flush threads.
                Thread(
                    target=self._flush,
                    args=(timeout_millis,),
                    daemon=True,
                    name="traceai-agentscope-flush",
                ).start()
        return self.flush_done.wait(seconds) and self.flush_ok

    def _flush(self, timeout_millis):
        try:
            self.flush_ok = bool(self.processor.force_flush(timeout_millis))
        except Exception:
            # Exception values/stacks may contain application data or credentials.
            _logger.warning("AgentScope exporter flush failed; details withheld")
            self.flush_ok = False
        finally:
            self.flush_done.set()

    def shutdown(self, timeout_millis):
        seconds = _timeout(timeout_millis)
        with self.lock:
            if not self.closed:
                self.closed = True
                Thread(
                    target=self._close, daemon=True, name="traceai-agentscope-close"
                ).start()
        return self.done.wait(seconds) and self.close_ok

    def _close(self):
        try:
            # Avoid concurrent exporter teardown and an outstanding explicit flush.
            self.flush_done.wait()
            self.processor.shutdown()
            self.close_ok = True
        except Exception:
            _logger.warning("AgentScope exporter shutdown failed; details withheld")
            self.close_ok = False
        finally:
            self.done.set()


class _Slot(SpanProcessor):
    def __init__(self, owns_provider):
        self.lock = RLock()
        self.pipeline = None
        self.closing = None
        self.stopped = False
        self.owns_provider = owns_provider
        self.open = OrderedDict()

    def on_start(self, span, parent_context=None):
        if not is_native(span):
            return
        with self.lock:
            if self.pipeline is not None:
                key = _key(span)
                # A collision cannot safely be attributed to either operation.
                if key in self.open:
                    self.open[key] = None
                else:
                    if len(self.open) >= _MAX_OPEN:
                        self.open.popitem(last=False)
                    self.open[key] = self.pipeline

    def on_end(self, span):
        with self.lock:
            pipeline = self.open.pop(_key(span), None)
        if pipeline is None:
            return
        try:
            projected = project(span, pipeline.project_name, pipeline.capture_content)
        except Exception:
            # Fail closed. No native content or exception values in diagnostics.
            _logger.warning(
                "AgentScope span projection failed; span dropped, details withheld"
            )
            return
        if projected is not None:
            with self.lock:
                if self.pipeline is pipeline:
                    # The SDK on_end only enqueues; no exporter/user code under this lock.
                    pipeline.processor.on_end(projected)

    def detach(self, pipeline, timeout_millis):
        _timeout(timeout_millis)
        with self.lock:
            if self.pipeline is pipeline:
                self.pipeline = None
                self.closing = pipeline
                self.open.clear()
            elif self.pipeline is None and self.closing is None:
                # A failed attachment may have registered an inactive slot.
                self.closing = pipeline
        return pipeline.shutdown(timeout_millis)

    def force_flush(self, timeout_millis=30000):
        with self.lock:
            pipeline = self.pipeline
        return True if pipeline is None else pipeline.force_flush(timeout_millis)

    def shutdown(self):
        with self.lock:
            self.stopped = True
            pipeline = self.pipeline or self.closing
        if pipeline is not None:
            self.detach(pipeline, 5000)


class Handle:
    """Own this destination, not the application's global provider.

    Both lifecycle methods return False on a deadline or cleanup failure. A flush
    reports queue drainage, not remote delivery. End spans before shutdown.
    """

    provider: TracerProvider
    owns_provider: bool

    def __init__(
        self, provider: TracerProvider, slot: _Slot, pipeline: _Pipeline
    ) -> None:
        self.provider = provider
        self.owns_provider = slot.owns_provider
        self._slot, self._pipeline = slot, pipeline

    def force_flush(self, timeout_millis: int = 5000) -> bool:
        return self._pipeline.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: int = 5000) -> bool:
        return self._slot.detach(self._pipeline, timeout_millis)


def configure(
    *,
    project_name: str,
    capture_content: bool = False,
    provider: TracerProvider | None = None,
    endpoint: str | None = None,
    headers: Mapping[str, str] | None = None,
    exporter: SpanExporter | None = None,
) -> Handle:
    """Reuse the global SDK provider or install one if none has been registered.

    A supplied provider must already be global, or is installed if OTel still uses
    its default proxy. Never replaces a live provider. A supplied exporter becomes
    this handle's responsibility only after validation; other processors are untouched.
    """
    if not isinstance(project_name, str) or not project_name.strip():
        raise ValueError("project_name must be a nonempty string")
    if type(capture_content) is not bool:
        raise TypeError("capture_content must be a bool")
    if endpoint is not None and (not isinstance(endpoint, str) or not endpoint):
        raise ValueError("endpoint must be a nonempty string")
    if headers is not None and (
        not isinstance(headers, Mapping)
        or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        )
    ):
        raise TypeError("headers must map strings to strings")
    if exporter is not None:
        if endpoint is not None or headers is not None:
            raise ValueError("use exporter or endpoint/headers, not both")
        if not all(
            callable(getattr(exporter, m, None))
            for m in ("export", "force_flush", "shutdown")
        ):
            raise TypeError(
                "exporter must implement the OpenTelemetry exporter interface"
            )
    signature = (
        project_name,
        capture_content,
        endpoint,
        tuple(sorted((headers or {}).items())),
        id(exporter) if exporter is not None else None,
    )
    global _configuring
    with _lock:
        if _configuring:
            raise RuntimeError("another AgentScope configure is in progress")
        _configuring = True
    pipeline = None
    try:
        current = trace.get_tracer_provider()
        owns_provider = provider is None and isinstance(current, ProxyTracerProvider)
        if provider is None:
            if owns_provider:
                provider = TracerProvider()
            elif isinstance(current, TracerProvider):
                provider = current
        if not isinstance(provider, TracerProvider):
            raise TypeError("AgentScope requires an OpenTelemetry SDK TracerProvider")
        if not isinstance(current, ProxyTracerProvider) and current is not provider:
            raise ValueError("provider must be the global provider used by AgentScope")
        if getattr(provider, "_default_processor", False):
            raise ValueError(
                "provider must not replace existing processors when one is added"
            )
        if getattr(provider, "_disabled", False):
            raise ValueError("the OpenTelemetry SDK is disabled")
        with _lock:
            slot = _slots.get(provider)
        if slot is not None:
            with slot.lock:
                if slot.stopped:
                    raise RuntimeError("provider has been shut down")
                if slot.pipeline is not None:
                    if slot.pipeline.signature != signature:
                        raise ValueError(
                            "already configured with different options; shut down first"
                        )
                    return Handle(provider, slot, slot.pipeline)
                if slot.closing is not None and not slot.closing.done.is_set():
                    raise RuntimeError("previous exporter is still closing")
        if exporter is None:
            from fi_instrumentation.otel import HTTPSpanExporter

            exporter = HTTPSpanExporter(endpoint=endpoint, headers=headers, timeout=10)
        pipeline = _Pipeline(exporter, signature, project_name, capture_content)
        if slot is None:
            slot = _Slot(owns_provider)
            try:
                provider.add_span_processor(slot)
            except BaseException:
                # SDK subclasses can attach and then raise. Reuse an attached inactive
                # slot on retry instead of silently accumulating duplicate processors.
                active = getattr(provider, "_active_span_processor", None)
                if slot in getattr(active, "_span_processors", ()):
                    with _lock:
                        _slots[provider] = slot
                raise
            with _lock:
                _slots[provider] = slot
        with slot.lock:
            if slot.stopped:
                raise RuntimeError("provider has been shut down")
            slot.pipeline = pipeline
        if isinstance(current, ProxyTracerProvider):
            trace.set_tracer_provider(provider)
            if trace.get_tracer_provider() is not provider:
                raise RuntimeError(
                    "another provider won global registration; configure again"
                )
        handle = Handle(provider, slot, pipeline)
        pipeline = None
        return handle
    finally:
        if pipeline is not None:
            if slot is not None:
                slot.detach(pipeline, 0)
            else:
                pipeline.shutdown(0)
        with _lock:
            _configuring = False
