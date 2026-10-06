"""OpenTelemetry instrumentation for the Exa client."""

import atexit
import logging
from importlib import import_module
from typing import Any, Collection, Dict, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_exa._wrappers import (
    AsyncOperationWrapper,
    AsyncStreamWrapper,
    OperationWrapper,
    StreamWrapper,
    end_open_streams,
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
    """Instrument Exa's search, contents, answer, and streaming APIs.

    ``instrument()`` accepts ``tracer_provider``, ``capture_urls`` and
    ``config`` (a ``fi_instrumentation.TraceConfig``; by default one built
    from the ``FI_*`` environment variables, such as ``FI_HIDE_INPUTS``).
    Spans come from an ``FITracer``, so the config's masking and PII
    redaction apply, and ``using_session`` / ``using_user`` /
    ``using_metadata`` attributes reach the Exa spans.
    """

    __slots__ = ("_original_methods",)

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
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )
        # Off by default: get_contents records only a URL count. With
        # capture_urls=True it also records up to 20 requested URLs, each
        # with the Exa key redacted and capped at 1 KB. Hidden inputs win.
        capture_urls = bool(kwargs.get("capture_urls", False))
        hide_inputs = bool(config.hide_inputs)
        api_module = import_module(_MODULE)
        self._original_methods: Dict[Tuple[str, str], Any] = {}

        for client_name in ("Exa", "AsyncExa"):
            is_async = client_name == "AsyncExa"
            for method_name, span_name in _OPERATIONS.items():
                options = {
                    "contents": method_name == "get_contents",
                    "capture_urls": capture_urls,
                    "hide_inputs": hide_inputs,
                }
                wrapper = (
                    AsyncOperationWrapper(tracer, span_name, **options)
                    if is_async
                    else OperationWrapper(tracer, span_name, **options)
                )
                self._wrap_method(api_module, client_name, method_name, wrapper)

            for method_name, span_name in _STREAM_OPERATIONS.items():
                wrapper = (
                    AsyncStreamWrapper(tracer, span_name, hide_inputs=hide_inputs)
                    if is_async
                    else StreamWrapper(tracer, span_name, hide_inputs=hide_inputs)
                )
                self._wrap_method(api_module, client_name, method_name, wrapper)

        # register() adds an atexit hook that shuts the provider down. atexit
        # runs hooks last-in first-out, so (re-)registering here makes open
        # stream spans end before that shutdown, not after it.
        atexit.unregister(end_open_streams)
        atexit.register(end_open_streams)
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
