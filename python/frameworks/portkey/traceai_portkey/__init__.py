import logging
from importlib import import_module
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from fi_instrumentation import FITracer, TraceConfig
from traceai_portkey._wrappers import _AsyncCompletionsWrapper, _CompletionsWrapper
from traceai_portkey.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("portkey_ai >= 1.1.2",)


class PortkeyInstrumentor(BaseInstrumentor):
    """Instrument native Portkey chat and prompt completions using the caller's provider."""

    __slots__ = ("_originals", "_tracer")

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider = (
            kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        )
        config = kwargs.get("config") or TraceConfig()
        assert isinstance(config, TraceConfig)
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )
        self._originals = []
        try:
            for resource in ("chat_complete", "generation"):
                module_name = f"portkey_ai.api_resources.apis.{resource}"
                module = import_module(module_name)
                for name, wrapper in (
                    ("Completions", _CompletionsWrapper),
                    ("AsyncCompletions", _AsyncCompletionsWrapper),
                ):
                    # Older Portkey releases have only synchronous resources.
                    cls = getattr(module, name, None)
                    if cls is None:
                        continue
                    self._originals.append((cls, cls.create))
                    wrap_function_wrapper(
                        module=module_name,
                        name=f"{name}.create",
                        wrapper=wrapper(tracer=self._tracer),
                    )
        except Exception:
            self._uninstrument()
            raise

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, original in getattr(self, "_originals", ()):
            cls.create = original
        self._originals = []
