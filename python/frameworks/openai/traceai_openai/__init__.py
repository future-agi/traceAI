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
from traceai_openai._client_provider import set_client_provider as set_client_provider
from traceai_openai._request import _AsyncRequest, _Request
from traceai_openai._request_context import capture_response_credentials
from traceai_openai.package import _instruments
from traceai_openai.version import __version__
from wrapt import wrap_function_wrapper

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "openai"


def _attachment_manager(kwargs: Any) -> Any:
    """The shared manager passed as ``attachments`` (or auto's ``attachment_manager``), if it can admit.

    A missing, disabled or pre-attachments core leaves tracing exactly as before.
    """
    manager = kwargs.get("attachments")
    if manager is None:
        manager = kwargs.get("attachment_manager")
    if manager is None:
        return None
    try:
        from fi_instrumentation.attachments.media_capture import active
    except ImportError:
        return None
    return manager if active(manager) else None


class OpenAIInstrumentor(BaseInstrumentor):  # type: ignore
    """
    An instrumentor for openai
    """

    # Legacy provider entry points only defer once extraction/privacy parity is
    # present, not merely because an older shared package happens to be enabled.
    _supports_deepseek_extensions = True

    __slots__ = (
        "_original_request",
        "_original_async_request",
        "_original_process_response",
        "_original_async_process_response",
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
        # The provider-owned shared manager (register(...).attachment_manager). Auto passes
        # it as attachment_manager; nothing is captured unless an enabled manager is supplied.
        attachments = _attachment_manager(kwargs)
        openai = import_module(_MODULE)
        self._original_request = openai.OpenAI.request
        self._original_async_request = openai.AsyncOpenAI.request
        wrap_function_wrapper(
            module=_MODULE,
            name="OpenAI.request",
            wrapper=_Request(tracer=tracer, openai=openai, config=config, attachments=attachments),
        )
        wrap_function_wrapper(
            module=_MODULE,
            name="AsyncOpenAI.request",
            wrapper=_AsyncRequest(
                tracer=tracer, openai=openai, config=config, attachments=attachments
            ),
        )
        self._original_process_response = openai.OpenAI._process_response
        self._original_async_process_response = openai.AsyncOpenAI._process_response
        for client in ("OpenAI", "AsyncOpenAI"):
            wrap_function_wrapper(
                module=_MODULE,
                name=f"{client}._process_response",
                wrapper=capture_response_credentials,
            )
        if Protect is not None:
            self._original_protect = Protect.protect
            wrap_function_wrapper(
                module="fi.evals",
                name="Protect.protect",
                wrapper=GuardrailProtectWrapper(tracer),
            )
        else:
            self._original_protect = None

    def _uninstrument(self, **kwargs: Any) -> None:
        openai = import_module(_MODULE)
        openai.OpenAI.request = self._original_request
        openai.AsyncOpenAI.request = self._original_async_request
        openai.OpenAI._process_response = self._original_process_response
        openai.AsyncOpenAI._process_response = self._original_async_process_response


_ATTACHMENT_EXPORTS = (
    "capture_openai_attachments",
    "project_openai_media",
    "capture_openai_speech",
    "observe_openai_speech",
    "capture_openai_audio_input",
    "openai_attachment_capabilities",
)


def __getattr__(name: str) -> Any:
    # Lazy: these need a fi-instrumentation-otel release with attachments, which
    # plain tracing does not.
    if name in _ATTACHMENT_EXPORTS:
        from traceai_openai import _attachments

        return getattr(_attachments, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
