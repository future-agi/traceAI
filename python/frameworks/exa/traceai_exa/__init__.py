"""OpenTelemetry instrumentation for the Exa client."""

import logging
from importlib import import_module
from typing import Any, Collection, Dict, Tuple

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_exa._wrappers import (
    AsyncOperationWrapper,
    AsyncStreamWrapper,
    OperationWrapper,
    StreamWrapper,
)
from traceai_exa.package import _instruments
from traceai_exa.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "exa_py.api"
_OPERATIONS = {
    "search": "exa.search",
    "get_contents": "exa.get_contents",
    "answer": "exa.answer",
    # The SDK keeps this deprecated alias for backwards compatibility. It is a
    # search operation, so it deliberately shares the search span name.
    "search_and_contents": "exa.search",
}
_STREAM_OPERATIONS = {
    "stream_search": "exa.search",
    "stream_answer": "exa.answer",
}


class ExaInstrumentor(BaseInstrumentor):
    """Instrument Exa's search, contents, answer, and streaming APIs."""

    __slots__ = ("_original_methods",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)
        api_module = import_module(_MODULE)
        self._original_methods: Dict[Tuple[str, str], Any] = {}

        for client_name in ("Exa", "AsyncExa"):
            is_async = client_name == "AsyncExa"
            for method_name, span_name in _OPERATIONS.items():
                wrapper = (
                    AsyncOperationWrapper(tracer, span_name)
                    if is_async
                    else OperationWrapper(tracer, span_name)
                )
                self._wrap_method(api_module, client_name, method_name, wrapper)

            for method_name, span_name in _STREAM_OPERATIONS.items():
                wrapper = (
                    AsyncStreamWrapper(tracer, span_name)
                    if is_async
                    else StreamWrapper(tracer, span_name)
                )
                self._wrap_method(api_module, client_name, method_name, wrapper)

        logger.debug("Exa instrumentation enabled")

    def _wrap_method(
        self,
        api_module: Any,
        client_name: str,
        method_name: str,
        wrapper: Any,
    ) -> None:
        client = getattr(api_module, client_name)
        if not hasattr(client, method_name):
            return

        self._original_methods[(client_name, method_name)] = getattr(client, method_name)
        wrap_function_wrapper(
            api_module,
            "{0}.{1}".format(client_name, method_name),
            wrapper,
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        api_module = import_module(_MODULE)
        for (client_name, method_name), original in self._original_methods.items():
            setattr(getattr(api_module, client_name), method_name, original)
        self._original_methods.clear()
        logger.debug("Exa instrumentation disabled")


__all__ = ["ExaInstrumentor", "__version__"]
