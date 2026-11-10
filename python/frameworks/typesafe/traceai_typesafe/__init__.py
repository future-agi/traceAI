"""OpenTelemetry instrumentation for the TypeSafe AI (Jev) Python SDK.

``TypeSafeInstrumentor`` wraps ``typesafe_sdk.TypeSafeClient.system_one`` and
``typesafe_sdk.AsyncTypeSafeClient.system_one``: one ``EVALUATOR`` span per
decision call. It never sets a base URL or a model, never calls the API
itself, and does not wrap ``client.models.list()`` or any HTTP library.

This is customer-side tracing of the System One decision API. Running Future
AGI evals with Jev as the judge model is a different feature.
"""

import logging
from importlib import import_module
from typing import Any, Collection, Dict, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_typesafe._wrappers import AsyncSystemOneWrapper, SyncSystemOneWrapper
from traceai_typesafe.package import _instruments
from traceai_typesafe.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Public import path, class, wrapper. GET /v1/models (client.models.list) is
# not a decision and is deliberately not wrapped.
_TARGETS: Tuple[Tuple[str, str, Any], ...] = (
    ("typesafe_sdk", "TypeSafeClient", SyncSystemOneWrapper),
    ("typesafe_sdk", "AsyncTypeSafeClient", AsyncSystemOneWrapper),
)
_METHOD = "system_one"


class TypeSafeInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Trace TypeSafe ``system_one`` decision calls on the sync and async clients.

    ``instrument()`` accepts:

    * ``tracer_provider``: the provider to use (default: the global one).
    * ``config``: a ``fi_instrumentation.TraceConfig``. Content is captured
      by default, as in every traceAI instrumentor. ``hide_inputs`` (or
      ``FI_HIDE_INPUTS=true``) drops the ``state`` and the questions;
      ``hide_outputs`` (or ``FI_HIDE_OUTPUTS=true``) drops the chosen labels
      and ``output.value``. Numeric answers, question names and types, model,
      usage and the request id are always recorded.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
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
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )
        self._originals: Dict[Tuple[str, str], Any] = {}
        for module_name, class_name, wrapper_class in _TARGETS:
            try:
                module = import_module(module_name)
                cls = getattr(module, class_name)
                original = cls.__dict__[_METHOD]
            except (ImportError, AttributeError, KeyError):
                logger.warning(
                    "typesafe_sdk.%s.%s was not found; it is not traced", class_name, _METHOD
                )
                continue
            wrap_function_wrapper(
                module_name, "{0}.{1}".format(class_name, _METHOD), wrapper_class(tracer, config)
            )
            self._originals[(module_name, class_name)] = (cls, original)

    def _uninstrument(self, **kwargs: Any) -> None:
        for (module_name, class_name), (cls, original) in getattr(self, "_originals", {}).items():
            try:
                setattr(cls, _METHOD, original)
            except Exception:
                logger.warning("Could not restore typesafe_sdk.%s.%s", class_name, _METHOD)
        self._originals = {}


__all__ = ["TypeSafeInstrumentor", "__version__"]
