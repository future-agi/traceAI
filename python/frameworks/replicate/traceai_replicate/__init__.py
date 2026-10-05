"""OpenTelemetry instrumentation for the official Replicate Python client."""

from __future__ import annotations

import atexit
import logging
from importlib import import_module
from typing import Any, Collection, List, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_replicate._wrappers import (
    CANCEL,
    CREATE,
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
)
from traceai_replicate.package import _instruments
from traceai_replicate.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_OPTIONS = frozenset({"tracer_provider", "config"})
# replicate/__init__.py binds these to default_client at import time.
_MODULE_FUNCTIONS = ("run", "async_run", "stream", "async_stream")


class ReplicateInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Trace ``replicate`` predictions: run, stream, create, wait and cancel.

    ``instrument(tracer_provider=..., config=TraceConfig(...))``. Trainings,
    file uploads, and reads (``predictions.get``/``list``, ``reload``) are not
    wrapped.
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
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )
        self._registry = PendingRegistry()
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
        atexit.unregister(self._end_pending)
        self._end_pending()


__all__ = ["ReplicateInstrumentor", "__version__"]
