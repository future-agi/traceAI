import logging
from importlib import import_module
from typing import Any, Collection

import logging
logger = logging.getLogger(__name__)
try:
    from fi.evals import Protect
except ImportError:
    logger.warning("ai-evaluation is not installed, please install it to trace protect")
    Protect = None
    pass
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from traceai_openai._request import _AsyncRequest, _Request
from traceai_openai.package import _instruments
from traceai_openai.version import __version__
from wrapt import wrap_function_wrapper

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "openai"


class OpenAIInstrumentor(BaseInstrumentor):  # type: ignore
    """
    An instrumentor for openai
    """

    __slots__ = (
        "_original_request",
        "_original_async_request",
    )

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )
        openai = import_module(_MODULE)
        self._original_request = openai.OpenAI.request
        self._original_async_request = openai.AsyncOpenAI.request
        wrap_function_wrapper(
            module=_MODULE,
            name="OpenAI.request",
            wrapper=_Request(tracer=tracer, openai=openai),
        )
        wrap_function_wrapper(
            module=_MODULE,
            name="AsyncOpenAI.request",
            wrapper=_AsyncRequest(tracer=tracer, openai=openai),
        )
        self._original_protect = Protect.protect
        wrap_function_wrapper(
            module="fi.evals",
            name="Protect.protect",
            wrapper=GuardrailProtectWrapper(tracer),
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        openai = import_module(_MODULE)
        openai.OpenAI.request = self._original_request
        openai.AsyncOpenAI.request = self._original_async_request
