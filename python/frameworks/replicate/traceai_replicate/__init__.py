"""OpenTelemetry instrumentation for the official Replicate Python client."""

from __future__ import annotations

import atexit
import inspect
import logging
import math
import time
from importlib import import_module
from typing import Any, Callable, Collection, Dict, List, Optional, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_replicate._stream_read import (
    DEFAULT_STREAM_FLUSH_WAIT_SECONDS,
    DEFAULT_STREAM_RELOAD_SECONDS,
    DEFAULT_STREAM_SHUTDOWN_WAIT_SECONDS,
    FLUSHED,
    MAX_STREAM_RELOAD_SECONDS,
    SHUTDOWN,
    UNINSTRUMENTED,
    StreamReads,
    end_stream_reads_at_exit,
    start_workers,
)
from traceai_replicate._wrappers import (
    CANCEL,
    CREATE,
    DEFAULT_MAX_PENDING_SECONDS,
    RUN,
    STREAM,
    WAIT,
    AsyncCreateWrapper,
    AsyncLifecycleWrapper,
    AsyncRunWrapper,
    CreateWrapper,
    LifecycleWrapper,
    PendingRegistry,
    RunWrapper,
    drain_released,
    end_open_iterators,
)
from traceai_replicate.package import _instruments
from traceai_replicate.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_OPTIONS = frozenset(
    {
        "tracer_provider",
        "config",
        "max_pending_seconds",
        "stream_reload_seconds",
        "stream_flush_wait_seconds",
        "stream_shutdown_wait_seconds",
    }
)
# replicate/__init__.py binds these to default_client at import time.
_MODULE_FUNCTIONS = ("run", "async_run", "stream", "async_stream")
# Provider methods that end held create spans before they run (see _hook_provider).
_PROVIDER_METHODS = ("force_flush", "shutdown")
_MISSING = object()


def _timeout_parameter(original: Any) -> Tuple[bool, Optional[float]]:
    """Whether ``original`` takes ``timeout_millis``, and its default."""
    try:
        parameters = inspect.signature(original).parameters
    except (TypeError, ValueError):
        return False, None
    parameter = parameters.get("timeout_millis")
    if parameter is None:
        return False, None
    return True, _milliseconds(parameter.default)


def _milliseconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


class _EndPendingFirst:
    """Stands in for ``force_flush`` / ``shutdown`` on one provider instance.

    Ends the create spans still held open on their predictions, and (with
    the opt-in read after a stream) the spans of completed streams whose read
    is still running, then calls the original, so those spans reach the
    processors before they flush or stop. ``shutdown`` also ends, as a best
    effort, a span whose ``wait()``/``cancel()`` is still running;
    ``force_flush`` leaves it to that call. ``fi_instrumentation.register()``'s
    SIGTERM/SIGINT handler shuts the provider down and exits; an ``atexit``
    hook alone would run after the processors had stopped and every held span
    would be dropped.

    Neither waits for reads in flight by default: like the SDK's own
    processors, they export what has ended, so each such span ends at once
    with what is known (``replicate.stream.read`` = ``flushed`` or
    ``shutdown``). With ``stream_flush_wait_seconds`` /
    ``stream_shutdown_wait_seconds`` each first waits that long for them,
    and never more than half of ``timeout_millis`` when there is one (passed,
    or the original's default, 30000 for the SDK's ``force_flush``). Whenever
    there is a timeout, the original gets only the time that is left, so the
    call never takes longer than asked, unless ending those spans itself
    takes longer (an exporter that runs inline, such as SimpleSpanProcessor's).
    The original's result is returned.
    """

    def __init__(
        self,
        original: Any,
        end_pending: Callable[[Optional[float]], Any],
        flush_wait: Callable[[], float],
    ) -> None:
        self.original = original
        self.end_pending = end_pending
        # How long to wait for reads in flight (0 by default), never more
        # than half the timeout when there is one.
        self.flush_wait = flush_wait
        self.enabled = True
        self.takes_timeout, self.default_timeout = _timeout_parameter(original)
        # Whatever is layered on top sees the original's name and signature.
        self.__wrapped__ = original
        for name in ("__name__", "__qualname__", "__doc__"):
            try:
                setattr(self, name, getattr(original, name))
            except (AttributeError, TypeError):
                pass

    def _timeout(self, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Tuple[bool, Optional[float]]:
        """(whether the caller passed ``timeout_millis``, the timeout in effect)."""
        if "timeout_millis" in kwargs:
            return True, _milliseconds(kwargs["timeout_millis"])
        if args:
            return True, _milliseconds(args[0])
        return False, self.default_timeout

    def _until(self, started: float, timeout: Optional[float]) -> float:
        half = None if timeout is None else timeout / 2000.0
        wait = self.flush_wait()
        return started + (wait if half is None else min(wait, half))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.enabled:
            return self.original(*args, **kwargs)
        started = time.monotonic()
        passed, timeout = self._timeout(args, kwargs)
        try:
            self.end_pending(self._until(started, timeout))
        except Exception:
            logger.debug("traceai-replicate: could not end held spans", exc_info=True)
        if timeout is not None and (passed or self.takes_timeout):
            # The time left, in whole milliseconds rounded up (so a short
            # timeout is not cut to 0, which the SDK treats as already over),
            # never more than the caller's.
            left = timeout - (time.monotonic() - started) * 1000.0
            remaining = min(timeout, math.ceil(left)) if left > 0 else 0
            if args and "timeout_millis" not in kwargs:
                args = (remaining,) + tuple(args[1:])
            else:
                kwargs["timeout_millis"] = remaining
        return self.original(*args, **kwargs)


def _max_pending_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            "max_pending_seconds must be a number of seconds (int or float), not {0}".format(
                type(value).__name__
            )
        )
    if not (math.isfinite(value) and value > 0):
        raise ValueError(
            "max_pending_seconds must be a positive, finite number of seconds, not {0!r}".format(
                value
            )
        )
    return float(value)


def _seconds_up_to_max(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            "{0} must be a number of seconds (int or float), not {1}".format(name, type(value).__name__)
        )
    if not (math.isfinite(value) and 0 <= value <= MAX_STREAM_RELOAD_SECONDS):
        raise ValueError(
            "{0} must be a number of seconds from 0 to {1:g}, not {2!r}".format(
                name, MAX_STREAM_RELOAD_SECONDS, value
            )
        )
    return float(value)


class ReplicateInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Trace ``replicate`` predictions: run, stream, create, wait and cancel.

    ``instrument(tracer_provider=..., config=TraceConfig(...),
    max_pending_seconds=600, stream_reload_seconds=0,
    stream_flush_wait_seconds=0, stream_shutdown_wait_seconds=0)``.
    Trainings, file uploads, and reads (``predictions.get``/``list``,
    ``reload``) are not wrapped.

    ``stream_reload_seconds`` (0 to 10, default 0: off) opts into reading a
    streamed prediction once more after its ``done`` event (``GET
    /v1/predictions/{id}``: no new prediction and no charge, but it counts
    toward the account's API rate limit) to record its final status,
    ``predict_time`` and token counts, within that wall-time budget. Off, a
    stream span ends at ``done`` (or the end of iteration) on the caller's
    thread with what the stream gave, and no thread is started. On, the read
    runs on background workers through the package's own connection pool,
    only for replicate's own transport or a plain httpx one without a proxy
    (any other transport, a subclass, one changed on the instance, or a
    client with a proxy: no read, ``replicate.stream.skip_reason`` =
    ``custom_transport``), and not while an HTTP mock has replaced httpx's
    or httpcore's send path (``patched_http``) or the socket layer
    (``patched_socket``: mocket, httpretty, gevent); the span ends when the
    read returns or the budget runs out, as of the stream's end. No read is
    made for a span that is not recording.

    ``stream_flush_wait_seconds`` and ``stream_shutdown_wait_seconds`` (0 to
    10, default 0) are how long ``force_flush()`` (at most half its timeout)
    and ``shutdown()`` / interpreter exit wait for those reads before they end
    their spans with what is known; by default they wait for none.
    ``uninstrument()`` never waits.

    ``max_pending_seconds`` is the longest a create span is held open on its
    prediction for a ``wait()``/``cancel()``; an older one is ended, as of
    create time, the next time a traced call runs or the provider is flushed
    or shut down.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        unknown = sorted(set(kwargs) - _OPTIONS)
        if unknown:
            raise TypeError(
                "ReplicateInstrumentor.instrument() got unexpected option(s): {0}".format(
                    ", ".join(unknown)
                )
            )
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError(
                "config must be a fi_instrumentation.TraceConfig, not {0}".format(
                    type(config).__name__
                )
            )
        max_pending_seconds = _max_pending_seconds(
            kwargs.get("max_pending_seconds", DEFAULT_MAX_PENDING_SECONDS)
        )
        reload_seconds = _seconds_up_to_max(
            "stream_reload_seconds", kwargs.get("stream_reload_seconds", DEFAULT_STREAM_RELOAD_SECONDS)
        )
        flush_wait = _seconds_up_to_max(
            "stream_flush_wait_seconds",
            kwargs.get("stream_flush_wait_seconds", DEFAULT_STREAM_FLUSH_WAIT_SECONDS),
        )
        shutdown_wait = _seconds_up_to_max(
            "stream_shutdown_wait_seconds",
            kwargs.get("stream_shutdown_wait_seconds", DEFAULT_STREAM_SHUTDOWN_WAIT_SECONDS),
        )
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )
        self._registry = PendingRegistry(max_pending_seconds)
        self._reads = StreamReads(reload_seconds, flush_wait, shutdown_wait)
        self._originals: List[Tuple[Any, str, Any]] = []
        self._module_functions: List[Tuple[Any, str, Any, Any]] = []

        def make(cls: Any, span_name: str, operation: str, **options: Any) -> Any:
            return cls(tracer, config, self._registry, span_name, operation, **options)

        targets = [
            ("replicate.client", "Client", "run", make(RunWrapper, RUN, "run")),
            ("replicate.client", "Client", "async_run", make(AsyncRunWrapper, RUN, "run")),
            (
                "replicate.client",
                "Client",
                "stream",
                make(RunWrapper, STREAM, "stream", stream=True, reads=self._reads),
            ),
            (
                "replicate.client",
                "Client",
                "async_stream",
                make(AsyncRunWrapper, STREAM, "stream", stream=True, reads=self._reads),
            ),
            ("replicate.prediction", "Predictions", "create", make(CreateWrapper, CREATE, "create")),
            (
                "replicate.prediction",
                "Predictions",
                "async_create",
                make(AsyncCreateWrapper, CREATE, "create"),
            ),
            (
                "replicate.prediction",
                "Predictions",
                "cancel",
                make(LifecycleWrapper, CANCEL, "cancel", by_id=True),
            ),
            (
                "replicate.prediction",
                "Predictions",
                "async_cancel",
                make(AsyncLifecycleWrapper, CANCEL, "cancel", by_id=True),
            ),
            (
                "replicate.prediction",
                "Prediction",
                "wait",
                make(LifecycleWrapper, WAIT, "prediction", skip_terminal=True),
            ),
            (
                "replicate.prediction",
                "Prediction",
                "async_wait",
                make(AsyncLifecycleWrapper, WAIT, "prediction", skip_terminal=True),
            ),
            ("replicate.prediction", "Prediction", "cancel", make(LifecycleWrapper, CANCEL, "prediction")),
            (
                "replicate.prediction",
                "Prediction",
                "async_cancel",
                make(AsyncLifecycleWrapper, CANCEL, "prediction"),
            ),
            ("replicate.model", "ModelsPredictions", "create", make(CreateWrapper, CREATE, "models.create")),
            (
                "replicate.model",
                "ModelsPredictions",
                "async_create",
                make(AsyncCreateWrapper, CREATE, "models.create"),
            ),
            (
                "replicate.deployment",
                "DeploymentsPredictions",
                "create",
                make(CreateWrapper, CREATE, "deployments.create"),
            ),
            (
                "replicate.deployment",
                "DeploymentsPredictions",
                "async_create",
                make(AsyncCreateWrapper, CREATE, "deployments.create"),
            ),
            (
                "replicate.deployment",
                "DeploymentPredictions",
                "create",
                make(CreateWrapper, CREATE, "deployment.create"),
            ),
            (
                "replicate.deployment",
                "DeploymentPredictions",
                "async_create",
                make(AsyncCreateWrapper, CREATE, "deployment.create"),
            ),
        ]
        for module_name, class_name, method, wrapper in targets:
            self._wrap(module_name, class_name, method, wrapper)
        self._rebind_module_functions()
        self._hook_provider(tracer_provider)
        if self._reads.budget > 0 and self._reads.hooked:
            # Only with the opt-in read: the read loop and the finisher start
            # here, so completing a stream never starts a thread on the
            # caller's path. With the read off (the default) no thread starts.
            start_workers()
        # Fallbacks for a provider whose methods could not be hooked; registered
        # after register(), so they run before the provider's own atexit
        # shutdown. drain_released, end_open_iterators and
        # end_stream_reads_at_exit stay registered after uninstrument() for a
        # stream that is still open then and is released, still open at exit,
        # or completes, later; none waits.
        for fallback in (drain_released, end_open_iterators, end_stream_reads_at_exit):
            atexit.unregister(fallback)
            atexit.register(fallback)
        atexit.register(self._at_exit)

    def _wrap(self, module_name: str, class_name: str, method: str, wrapper: Any) -> None:
        try:
            module = import_module(module_name)
            owner = getattr(module, class_name)
            original = owner.__dict__[method]
        except (ImportError, AttributeError, KeyError):
            logger.warning(
                "traceai-replicate: %s.%s.%s was not found in the installed replicate; "
                "it is not traced",
                module_name,
                class_name,
                method,
            )
            return
        wrap_function_wrapper(module, "{0}.{1}".format(class_name, method), wrapper)
        self._originals.append((owner, method, original))

    def _rebind_module_functions(self) -> None:
        """Route ``replicate.run`` & co. through the wrapped methods.

        ``replicate/__init__.py`` stores ``default_client.run`` (a bound method
        of the original function), so a class-level wrapper alone would miss
        it. Only a binding that still points at the original method is
        replaced, and ``uninstrument()`` puts back that exact object.
        """
        try:
            package = import_module("replicate")
            client_class = import_module("replicate.client").Client
        except (ImportError, AttributeError):
            return
        originals = {
            method: original
            for owner, method, original in self._originals
            if owner is client_class
        }
        for name in _MODULE_FUNCTIONS:
            current = getattr(package, name, None)
            owner = getattr(current, "__self__", None)
            if (
                name in originals
                and getattr(current, "__func__", None) is originals[name]
                and isinstance(owner, client_class)
            ):
                rebound = getattr(owner, name)
                setattr(package, name, rebound)
                self._module_functions.append((package, name, current, rebound))

    def _hook_provider(self, provider: Any) -> None:
        """End held create spans before ``provider.force_flush()``/``shutdown()``.

        Only this provider instance is changed. A provider without these
        methods, or whose attributes cannot be set, is left as it is; the
        ``atexit`` fallback still ends held spans on a normal exit.
        """
        self._provider_hooks: List[Tuple[Any, str, Any, _EndPendingFirst]] = []
        # A flush leaves a span whose wait()/cancel() is running to that call;
        # shutdown ends it too, or it would be dropped once the processors stop.
        ends = {"force_flush": self._expire_held, "shutdown": self._shut_down}
        reads = self._reads
        waits = {"force_flush": lambda: reads.flush_wait, "shutdown": lambda: reads.shutdown_wait}
        for name in _PROVIDER_METHODS:
            try:
                original = getattr(provider, name, None)
                if not callable(original):
                    continue
                try:
                    previous = vars(provider).get(name, _MISSING)
                except TypeError:  # no __dict__: setattr below fails as well
                    previous = _MISSING
                hook = _EndPendingFirst(original, ends[name], waits[name])
                setattr(provider, name, hook)
            except Exception:
                logger.debug(
                    "traceai-replicate: cannot hook %s.%s; held create spans end at exit",
                    type(provider).__name__,
                    name,
                    exc_info=True,
                )
                continue
            self._provider_hooks.append((provider, name, previous, hook))
        # The opt-in read after a stream only where a flush and a shutdown can
        # end its span; otherwise a read could end it after the processors stopped.
        self._reads.hooked = bool(self._provider_hooks)

    def _unhook_provider(self) -> None:
        for provider, name, previous, hook in reversed(getattr(self, "_provider_hooks", [])):
            hook.enabled = False  # a copy someone kept, or a hook layered on ours, passes through
            try:
                if vars(provider).get(name) is not hook:
                    continue  # replaced after instrument(): not ours to undo
                if previous is _MISSING:
                    delattr(provider, name)
                else:
                    setattr(provider, name, previous)
            except Exception:
                logger.debug("traceai-replicate: could not restore %s", name, exc_info=True)
        self._provider_hooks = []
        reads = getattr(self, "_reads", None)
        if reads is not None:
            reads.hooked = False

    def _end_pending(self, until: Optional[float] = None) -> None:
        """Provider shutdown and exit: every held span, the streams whose read
        is still running (waited for no later than ``until``, by default not
        at all, then ended as ``shutdown``), the spans whose wait()/cancel()
        is still running, and the streams and run iterators still open
        (ended as cancelled, see :func:`end_open_iterators`)."""
        registry = getattr(self, "_registry", None)
        if registry is not None:
            registry.end_all(time.monotonic() if until is None else until, outcome=SHUTDOWN)
        end_open_iterators()

    def _at_exit(self) -> None:
        """Interpreter exit: as shutdown, waiting ``stream_shutdown_wait_seconds`` at most."""
        reads = getattr(self, "_reads", None)
        wait = reads.shutdown_wait if reads is not None else 0.0
        self._end_pending(time.monotonic() + wait)

    def _shut_down(self, until: Optional[float] = None) -> None:
        """Provider shutdown: no new read (nothing would be exported), then
        :meth:`_end_pending`."""
        reads = getattr(self, "_reads", None)
        if reads is not None:
            reads.shut_down = True
        self._end_pending(until)

    def _expire_held(self, until: Optional[float] = None) -> None:
        """Flush: every held span, and the streams whose read is still running
        (waited for no later than ``until``, then ended as ``flushed``). A
        running wait()/cancel() keeps its span and ends it when it returns."""
        registry = getattr(self, "_registry", None)
        if registry is not None:
            registry.expire_all(until=until, outcome=FLUSHED)

    def _uninstrument(self, **kwargs: Any) -> None:
        for package, name, original, rebound in reversed(getattr(self, "_module_functions", [])):
            if getattr(package, name, None) is rebound:
                setattr(package, name, original)
        for owner, method, original in reversed(getattr(self, "_originals", [])):
            setattr(owner, method, original)
        self._module_functions = []
        self._originals = []
        reads = getattr(self, "_reads", None)
        if reads is not None:
            reads.active = False  # a stream that completes from now on makes no read
        self._unhook_provider()
        atexit.unregister(self._at_exit)
        # Held create spans end now, and so does every stream span whose read
        # is still running, at once and with what is known
        # (``uninstrumented``): nothing waits, and a shutdown right after
        # exports them.
        registry = getattr(self, "_registry", None)
        if registry is not None:
            registry.expire_all(until=time.monotonic(), outcome=UNINSTRUMENTED)


__all__ = ["ReplicateInstrumentor", "__version__"]
