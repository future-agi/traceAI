"""OpenTelemetry instrumentation for the Firecrawl v2 client."""

import logging
from typing import Any, Collection, Dict, Tuple

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper
from fi_instrumentation import FITracer
from fi_instrumentation.instrumentation import TraceConfig

from traceai_firecrawl._wrappers import AsyncOperationWrapper, OperationWrapper, _State
from traceai_firecrawl.package import _instruments
from traceai_firecrawl.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# firecrawl-py 4.46.2 binds scrape/search/map/crawl onto the v2 client, and the
# top-level Firecrawl delegates to it (firecrawl/client.py assigns
# self.scrape = self._v2_client.scrape). The methods are not on the Firecrawl
# class, so the wrapper attaches to the v2 client. Wrapping that class covers
# both `from firecrawl import Firecrawl` and direct FirecrawlClient use.
_SYNC_MODULE = "firecrawl.v2.client"
_SYNC_CLASS = "FirecrawlClient"
_ASYNC_MODULE = "firecrawl.v2.client_async"
_ASYNC_CLASS = "AsyncFirecrawlClient"

_OPERATIONS = {
    "scrape": "firecrawl.scrape",
    "search": "firecrawl.search",
    "map": "firecrawl.map",
    "crawl": "firecrawl.crawl",
    "start_crawl": "firecrawl.start_crawl",
    "get_crawl_status": "firecrawl.get_crawl_status",
    "cancel_crawl": "firecrawl.cancel_crawl",
    "extract": "firecrawl.extract",
    "start_extract": "firecrawl.start_extract",
    "get_extract_status": "firecrawl.get_extract_status",
    "batch_scrape": "firecrawl.batch_scrape",
    "start_batch_scrape": "firecrawl.start_batch_scrape",
    # Only exposed by AsyncFirecrawlClient; _wrap skips absent methods.
    "wait_batch_scrape": "firecrawl.wait_batch_scrape",
    "get_batch_scrape_status": "firecrawl.get_batch_scrape_status",
    "get_batch_scrape_status_page": "firecrawl.get_batch_scrape_status_page",
    "get_batch_scrape_errors": "firecrawl.get_batch_scrape_errors",
    "cancel_batch_scrape": "firecrawl.cancel_batch_scrape",
    "agent": "firecrawl.agent",
    "start_agent": "firecrawl.start_agent",
    "get_agent_status": "firecrawl.get_agent_status",
    "cancel_agent": "firecrawl.cancel_agent",
    "browser": "firecrawl.browser",
    "browser_execute": "firecrawl.browser_execute",
    "delete_browser": "firecrawl.delete_browser",
    "list_browsers": "firecrawl.list_browsers",
}


class FirecrawlInstrumentor(BaseInstrumentor):
    """Instrument Firecrawl v2 scraping, extraction, agent and browser operations."""

    __slots__ = ("_original_methods",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = FITracer(trace_api.get_tracer(__name__, __version__, tracer_provider), config=config)
        self._original_methods: Dict[Tuple[str, str, str], Any] = {}
        self._state = _State()

        for module_name, class_name, is_async in (
            (_SYNC_MODULE, _SYNC_CLASS, False),
            (_ASYNC_MODULE, _ASYNC_CLASS, True),
        ):
            for method_name, span_name in _OPERATIONS.items():
                wrapper = (
                    AsyncOperationWrapper(tracer, span_name, method_name, config, self._state)
                    if is_async
                    else OperationWrapper(tracer, span_name, method_name, config, self._state)
                )
                self._wrap(module_name, class_name, method_name, wrapper)

        logger.debug("Firecrawl instrumentation enabled")

    def _wrap(self, module_name: str, class_name: str, method_name: str, wrapper: Any) -> None:
        from importlib import import_module

        module = import_module(module_name)
        client = getattr(module, class_name)
        # Only a method defined on this class itself, so an inherited one is
        # never wrapped twice and uninstrument() restores exactly.
        original = vars(client).get(method_name)
        if original is None:
            return

        self._original_methods[(module_name, class_name, method_name)] = original
        # wrapt 2.5.0 rejects keyword arguments, so these are positional.
        wrap_function_wrapper(module, "{0}.{1}".format(class_name, method_name), wrapper)

    def _uninstrument(self, **kwargs: Any) -> None:
        from importlib import import_module

        state = getattr(self, "_state", None)
        if state is not None:
            # A facade built while instrumented holds bound wrappers; stop them.
            state.enabled = False
        for (module_name, class_name, method_name), original in self._original_methods.items():
            setattr(getattr(import_module(module_name), class_name), method_name, original)
        self._original_methods.clear()
        logger.debug("Firecrawl instrumentation disabled")


__all__ = ["FirecrawlInstrumentor", "__version__"]
