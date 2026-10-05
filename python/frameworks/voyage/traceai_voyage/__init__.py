"""OpenTelemetry instrumentation for the official Voyage AI Python client.

``VoyageInstrumentor`` wraps ``voyageai.Client`` and ``voyageai.AsyncClient``
``embed`` (one EMBEDDING span per call) and ``rerank`` (one RERANKER span per
call). It never sets ``base_url``: the client picks its host from the key.
"""

import inspect
import logging
from importlib import import_module
from typing import Any, Collection, Dict, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_voyage._wrappers import EMBED, RERANK, AsyncWrapper, Operation, SyncWrapper
from traceai_voyage.package import _instruments
from traceai_voyage.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# (module, class, method, operation). multimodal_embed and contextualized_embed
# are deliberately not wrapped in v1 (PRD R-07): they produce no span.
_TARGETS: Tuple[Tuple[str, str, str, Operation], ...] = (
    ("voyageai.client", "Client", "embed", EMBED),
    ("voyageai.client", "Client", "rerank", RERANK),
    ("voyageai.client_async", "AsyncClient", "embed", EMBED),
    ("voyageai.client_async", "AsyncClient", "rerank", RERANK),
)


class VoyageInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Trace Voyage ``embed`` and ``rerank`` on the sync and async clients.

    ``instrument()`` accepts:

    * ``tracer_provider``: the provider to use (default: the global one).
    * ``config``: a ``fi_instrumentation.TraceConfig``. ``hide_inputs`` (or
      ``hide_input_text``, or ``FI_HIDE_INPUTS``) drops the rerank query and
      any captured texts and documents; ``hide_outputs`` (or
      ``FI_HIDE_OUTPUTS``) drops the rerank scores. Hiding inputs keeps the
      scores. ``pii_redaction`` applies to every recorded value.
    * ``capture_content``: ``False`` by default. By default only the rerank
      span records content: the query (``reranker.query`` and a plain-text
      ``input.value``) and the scores (``output.value``), as PRD J2 / AC-03
      require; embed spans record none. ``True`` also records the embed texts
      and the rerank documents. Embedding vectors are never recorded.
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
        capture_content = kwargs.get("capture_content", False)
        if not isinstance(capture_content, bool):
            raise TypeError(
                "capture_content must be a bool, not {0}".format(type(capture_content).__name__)
            )
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )

        self._originals: Dict[Tuple[str, str, str], Any] = {}
        for module_name, class_name, method_name, operation in _TARGETS:
            try:
                module = import_module(module_name)
                original = getattr(module, class_name).__dict__[method_name]
            except (ImportError, AttributeError, KeyError):
                logger.warning(
                    "voyageai %s.%s.%s was not found; it is not traced",
                    module_name,
                    class_name,
                    method_name,
                )
                continue
            wrapper_class = AsyncWrapper if inspect.iscoroutinefunction(original) else SyncWrapper
            wrap_function_wrapper(
                module,
                "{0}.{1}".format(class_name, method_name),
                wrapper_class(tracer, operation, config, capture_content),
            )
            self._originals[(module_name, class_name, method_name)] = original
        logger.debug("Voyage instrumentation enabled")

    def _uninstrument(self, **kwargs: Any) -> None:
        originals = getattr(self, "_originals", {})
        for (module_name, class_name, method_name), original in originals.items():
            try:
                setattr(getattr(import_module(module_name), class_name), method_name, original)
            except Exception:
                logger.warning(
                    "Could not restore voyageai %s.%s.%s", module_name, class_name, method_name
                )
        originals.clear()
        logger.debug("Voyage instrumentation disabled")


__all__ = ["VoyageInstrumentor", "__version__"]
