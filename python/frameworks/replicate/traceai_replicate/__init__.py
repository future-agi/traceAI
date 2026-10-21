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
    DEFAULT_STREAM_RELOAD_SECONDS,
    MAX_STREAM_RELOAD_SECONDS,
    StreamReads,
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
    settle_stream_reads,
)
from traceai_replicate.package import _instruments
from traceai_replicate.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_OPTIONS = frozenset({"tracer_provider", "config", "max_pending_seconds", "stream_reload_seconds"})
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
    default = parameter.default
    usable = isinstance(default, (int, float)) and not isinstance(default, bool)
    return True, float(default) if usable else None


def _milliseconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


class _EndPendingFirst:
    """Stands in for ``force_flush`` / ``shutdown`` on one provider instance.

    Ends the create spans still held open on their predictions, and the
    spans of completed streams whose read is still running, then calls the
    original, so those spans reach the processors before they flush or stop.
    ``shutdown`` also ends, as a best effort, a span whose
    ``wait()``/``cancel()`` is still running; ``force_flush`` leaves it to
    that call. ``fi_instrumentation.register()``'s SIGTERM/SIGINT handler
    shuts the provider down and exits; an ``atexit`` hook alone would run
    after the processors had stopped and every held span would be dropped.

    The caller's timeout is honoured. With ``timeout_millis`` (passed, or the
    original's default, 30000 for the SDK's ``force_flush``), reads in flight
    are waited for at most half of it, never past their own budget; a read
    still running then is cut short (its span ends with what is known), and
    the original gets the time that is left, so the whole call never takes
    longer than asked. Without one (the SDK's ``shutdown()``), the wait is
    bounded by the reads' budgets (``stream_reload_seconds``, at most 10 s).
    The original's result is returned.
    """

    def __init__(self, original: Any, end_pending: Callable[[Optional[float]], Any]) -> None:
        self.original = original
        self.end_pending = end_pending
        self.enabled = True
        self.takes_timeout, self.default_timeout = _timeout_parameter(original)

    def _timeout(self, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Optional[float]:
        if "timeout_millis" in kwargs:
            return _milliseconds(kwargs["timeout_millis"])
        if args:
            return _milliseconds(args[0])
        return self.default_timeout

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.enabled:
            return self.original(*args, **kwargs)
        started = time.monotonic()
        timeout = self._timeout(args, kwargs)
        until = None if timeout is None else started + timeout / 2000.0
        try:
            self.end_pending(until)
        except Exception:
            logger.debug("traceai-replicate: could not end held spans", exc_info=True)
        if timeout is not None and self.takes_timeout:
            left = max(0, int(timeout - (time.monotonic() - started) * 1000.0))
            if "timeout_millis" in kwargs or not args:
                kwargs["timeout_millis"] = left
            else:
                args = (left,) + tuple(args[1:])
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


def _stream_reload_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            "stream_reload_seconds must be a number of seconds (int or float), not {0}".format(
                type(value).__name__
            )
        )
    if not (math.isfinite(value) and 0 <= value <= MAX_STREAM_RELOAD_SECONDS):
        raise ValueError(
            "stream_reload_seconds must be a number of seconds from 0 to {0:g}, not {1!r}".format(
                MAX_STREAM_RELOAD_SECONDS, value
            )
        )
    return float(value)


class ReplicateInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Trace ``replicate`` predictions: run, stream, create, wait and cancel.

    ``instrument(tracer_provider=..., config=TraceConfig(...),
    max_pending_seconds=600, stream_reload_seconds=2.0)``. Trainings, file
    uploads, and reads (``predictions.get``/``list``, ``reload``) are not
    wrapped.

    ``stream_reload_seconds`` (0 to 10) is the wall-time budget for reading a
    streamed prediction once more after its ``done`` event (``GET
    /v1/predictions/{id}``: no new prediction and no charge, but it counts
    toward the account's API rate limit) to record its final status,
    ``predict_time`` and token counts; ``0`` turns the read off. The read
    runs on background workers, so the caller's iteration is never held up;
    the span ends when the read returns or the budget runs out, as of the
    stream's end. No read is made for a span that is not recording.

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
        reload_seconds = _stream_reload_seconds(
            kwargs.get("stream_reload_seconds", DEFAULT_STREAM_RELOAD_SECONDS)
        )
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )
        self._registry = PendingRegistry(max_pending_seconds)
        self._reads = StreamReads(reload_seconds)
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
        # Fallbacks for a provider whose methods could not be hooked; registered
        # after register(), so they run before the provider's own atexit
        # shutdown. drain_released and settle_stream_reads stay registered
        # after uninstrument() for a stream that is still open then and is
        # released, or completes, later.
        atexit.unregister(drain_released)
        atexit.register(drain_released)
        atexit.unregister(settle_stream_reads)
        atexit.register(settle_stream_reads)
        atexit.register(self._end_pending)

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
        for name in _PROVIDER_METHODS:
            try:
                original = getattr(provider, name, None)
                if not callable(original):
                    continue
                try:
                    previous = vars(provider).get(name, _MISSING)
                except TypeError:  # no __dict__: setattr below fails as well
                    previous = _MISSING
                hook = _EndPendingFirst(original, ends[name])
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
        # Reads after a stream only where a flush and a shutdown wait for them.
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
        is still running, and the spans whose wait()/cancel() is still running."""
        registry = getattr(self, "_registry", None)
        if registry is not None:
            registry.end_all(until)

    def _shut_down(self, until: Optional[float] = None) -> None:
        """Provider shutdown: no new read (nothing would be exported), then
        :meth:`_end_pending`."""
        reads = getattr(self, "_reads", None)
        if reads is not None:
            reads.shut_down = True
        self._end_pending(until)

    def _expire_held(self, until: Optional[float] = None) -> None:
        """Flush: every held span, and the streams whose read is still running
        (waited for no later than ``until``). A running wait()/cancel() keeps
        its span and ends it when it returns."""
        registry = getattr(self, "_registry", None)
        if registry is not None:
            registry.expire_all(until=until)

    def _uninstrument(self, **kwargs: Any) -> None:
        for package, name, original, rebound in reversed(getattr(self, "_module_functions", [])):
            if getattr(package, name, None) is rebound:
                setattr(package, name, original)
        for owner, method, original in reversed(getattr(self, "_originals", [])):
            setattr(owner, method, original)
        self._module_functions = []
        self._originals = []
        self._unhook_provider()
        atexit.unregister(self._end_pending)
        # Held create spans end now. A stream whose read is still running is
        # not waited for: the read's worker ends its span on the provider,
        # which is still live, when the read returns or its budget runs out.
        registry = getattr(self, "_registry", None)
        if registry is not None:
            registry.expire_all(settle=False)


__all__ = ["ReplicateInstrumentor", "__version__"]
