import logging
from importlib import import_module
from typing import Any, Collection

logger = logging.getLogger(__name__)

try:
    from fi.evals import Protect
except ImportError:
    logger.debug("ai-evaluation is not installed")
    Protect = None

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_cohere._wrappers import (
    _AsyncChatStreamWrapper,
    _AsyncChatWrapper,
    _AsyncEmbedWrapper,
    _AsyncRerankWrapper,
    _ChatStreamWrapper,
    _ChatWrapper,
    _EmbedWrapper,
    _RerankWrapper,
)
from traceai_cohere.version import __version__

logger.addHandler(logging.NullHandler())

_instruments = ("cohere >= 5.0.0",)


class CohereInstrumentor(BaseInstrumentor):
    """Instrument v1/v2 Cohere chat, streaming, embeddings and reranking."""

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
        self._original_methods = []
        cohere_module = import_module("cohere")
        sync = (_ChatWrapper, _ChatStreamWrapper, _EmbedWrapper, _RerankWrapper)
        asynchronous = (
            _AsyncChatWrapper,
            _AsyncChatStreamWrapper,
            _AsyncEmbedWrapper,
            _AsyncRerankWrapper,
        )
        patched = set()
        for client_name, wrappers in (
            ("Client", sync),
            ("AsyncClient", asynchronous),
            ("ClientV2", sync),
            ("AsyncClientV2", asynchronous),
        ):
            client = getattr(cohere_module, client_name, None)
            if client is None:  # v2 does not exist at the supported SDK floor.
                continue
            for method, wrapper in zip(
                ("chat", "chat_stream", "embed", "rerank"), wrappers
            ):
                # ClientV2 uses distinct v2 base methods; older SDKs inherit some
                # v1 methods. Patch each implementation only once, at its owner.
                owner = next(
                    (cls for cls in client.__mro__ if method in vars(cls)), None
                )
                if owner is None or (owner, method) in patched:
                    continue
                patched.add((owner, method))
                self._original_methods.append((owner, method, vars(owner)[method]))
                wrap_function_wrapper(owner, method, wrapper(tracer=self._tracer))

        if Protect is not None:
            self._original_methods.append((Protect, "protect", Protect.protect))
            wrap_function_wrapper(
                Protect, "protect", GuardrailProtectWrapper(tracer=self._tracer)
            )

    def _uninstrument(self, **kwargs: Any) -> None:
        for owner, method, original in reversed(self._original_methods):
            setattr(owner, method, original)
        self._original_methods.clear()
