"""OpenTelemetry instrumentation for the official Replicate Python client."""

from __future__ import annotations

import atexit
import logging
import math
from importlib import import_module
from typing import Any, Collection, List, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

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
)
from traceai_replicate.package import _instruments
from traceai_replicate.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_OPTIONS = frozenset({"tracer_provider", "config", "max_pending_seconds"})
# replicate/__init__.py binds these to default_client at import time.
_MODULE_FUNCTIONS = ("run", "async_run", "stream", "async_stream")
# Provider methods that end held create spans before they run (see _hook_provider).
_PROVIDER_METHODS = ("force_flush", "shutdown")
_MISSING = object()


class _EndPendingFirst:
    """Stands in for ``force_flush`` / ``shutdown`` on one provider instance.

    Ends the create spans still held open on their predictions, then calls
    the original, so those spans reach the processors before they flush or
    stop. ``fi_instrumentation.register()``'s SIGTERM/SIGINT handler shuts
    the provider down and exits; an ``atexit`` hook alone would run after the
    processors had stopped and every held span would be dropped.
    """

    def __init__(self, original: Any, end_pending: Any) -> None:
        self.original = original
        self.end_pending = end_pending
        self.enabled = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.enabled:
            try:
                self.end_pending()
            except Exception:
                logger.debug("traceai-replicate: could not end held spans", exc_info=True)
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


class ReplicateInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Trace ``replicate`` predictions: run, stream, create, wait and cancel.

    ``instrument(tracer_provider=..., config=TraceConfig(...),
    max_pending_seconds=600)``. Trainings, file uploads, and reads
    (``predictions.get``/``list``, ``reload``) are not wrapped.

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
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )
        self._registry = PendingRegistry(max_pending_seconds)
        self._originals: List[Tuple[Any, str, Any]] = []
        self._module_functions: List[Tuple[Any, str, Any, Any]] = []

        def make(cls: Any, span_name: str, operation: str, **options: Any) -> Any:
            return cls(tracer, config, self._registry, span_name, operation, **options)

        targets = [
            ("replicate.client", "Client", "run", make(RunWrapper, RUN, "run")),
            ("replicate.client", "Client", "async_run", make(AsyncRunWrapper, RUN, "run")),
            ("replicate.client", "Client", "stream", make(RunWrapper, STREAM, "stream", stream=True)),
            (
                "replicate.client",
                "Client",
                "async_stream",
                make(AsyncRunWrapper, STREAM, "stream", stream=True),
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
        # shutdown. drain_released stays registered after uninstrument() for a
        # stream that is still open then and released later.
        atexit.unregister(drain_released)
        atexit.register(drain_released)
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
        for name in _PROVIDER_METHODS:
            try:
                original = getattr(provider, name, None)
                if not callable(original):
                    continue
                try:
                    previous = vars(provider).get(name, _MISSING)
                except TypeError:  # no __dict__: setattr below fails as well
                    previous = _MISSING
                hook = _EndPendingFirst(original, self._end_pending)
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

    def _end_pending(self) -> None:
        registry = getattr(self, "_registry", None)
        if registry is not None:
            registry.expire_all()

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
        self._end_pending()


__all__ = ["ReplicateInstrumentor", "__version__"]
