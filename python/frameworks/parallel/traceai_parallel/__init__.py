"""OpenTelemetry instrumentation for Parallel Search and Extract (parallel-web).

Wraps ``Parallel.search``, ``Parallel.extract`` and their ``AsyncParallel``
twins. The Task API (``task_run``, ``task_group``), Monitor, FindAll and the
rest of ``client.beta`` are different products and are not traced.
"""

import logging
from importlib import import_module
from typing import Any, Collection, Dict, Tuple

from fi_instrumentation import TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_parallel._wrappers import (
    EXTRACT,
    SEARCH,
    AsyncOperationWrapper,
    OperationWrapper,
    Options,
    _State,
)
from traceai_parallel.package import _instruments
from traceai_parallel.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "parallel._client"
_CLIENTS = (("Parallel", False), ("AsyncParallel", True))
_OPERATIONS = (SEARCH, EXTRACT)


def _flag(kwargs: Dict[str, Any], name: str) -> bool:
    value = kwargs.get(name, False)
    if not isinstance(value, bool):
        raise TypeError("{0} must be a bool, got {1}".format(name, type(value).__name__))
    return value


class ParallelInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrument Parallel's Search and Extract client methods.

    ``instrument()`` accepts ``tracer_provider``, ``config`` (a
    ``fi_instrumentation.TraceConfig``; its ``hide_inputs`` / ``hide_outputs``
    are honoured), ``capture_urls`` and ``capture_objective`` (both off by
    default).
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError(
                "config must be a fi_instrumentation.TraceConfig, got {0}".format(
                    type(config).__name__
                )
            )
        options = Options(
            capture_urls=_flag(kwargs, "capture_urls"),
            capture_objective=_flag(kwargs, "capture_objective"),
            hide_inputs=bool(config.hide_inputs),
            hide_outputs=bool(config.hide_outputs),
        )
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        self._originals: Dict[Tuple[str, str], Any] = {}
        self._state = _State()
        try:
            client_module = import_module(_MODULE)
        except ImportError:
            logger.warning("Parallel instrumentation skipped: %s is not importable", _MODULE)
            return

        for client_name, is_async in _CLIENTS:
            client = getattr(client_module, client_name, None)
            for operation in _OPERATIONS:
                if client is None or operation not in vars(client):
                    logger.warning(
                        "Parallel instrumentation skipped %s.%s: not defined on the class",
                        client_name,
                        operation,
                    )
                    continue
                wrapper_type = AsyncOperationWrapper if is_async else OperationWrapper
                wrapper = wrapper_type(tracer, operation, options, self._state)
                self._originals[(client_name, operation)] = vars(client)[operation]
                wrap_function_wrapper(
                    client_module, "{0}.{1}".format(client_name, operation), wrapper
                )
        logger.debug("Parallel instrumentation enabled")

    def _uninstrument(self, **kwargs: Any) -> None:
        state = getattr(self, "_state", None)
        if state is not None:
            # Bound copies taken while instrumented (with_raw_response) stop tracing.
            state.enabled = False
        originals = getattr(self, "_originals", {})
        if originals:
            client_module = import_module(_MODULE)
            for (client_name, operation), original in originals.items():
                setattr(getattr(client_module, client_name), operation, original)
        self._originals = {}
        logger.debug("Parallel instrumentation disabled")


__all__ = ["ParallelInstrumentor", "__version__"]
