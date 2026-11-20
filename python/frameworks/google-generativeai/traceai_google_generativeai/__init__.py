"""OpenTelemetry instrumentation for the legacy ``google.generativeai`` SDK.

``GenerativeModel.generate_content`` (and its async form, with or without ``stream=True``) gets one
LLM span; ``ChatSession.send_message`` is covered through that call, one span per turn with the
chat history as input. ``embed_content`` and ``embed_content_async`` get EMBEDDING spans.
``count_tokens`` is not traced.
"""
import logging
from typing import Any, Collection, Dict, Tuple

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.trace import get_tracer, get_tracer_provider
from wrapt import wrap_function_wrapper

from fi_instrumentation import FITracer, TraceConfig
from traceai_google_generativeai.package import _instruments
from traceai_google_generativeai.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODELS = "google.generativeai.generative_models"
# The embedding functions are bound in two places; wrap both so either import style is traced.
_TARGETS: Tuple[Tuple[str, str, str], ...] = (
    (_MODELS, "GenerativeModel.generate_content", "GenerateContent"),
    (_MODELS, "GenerativeModel.generate_content_async", "AsyncGenerateContent"),
    ("google.generativeai.embedding", "embed_content", "EmbedContent"),
    ("google.generativeai.embedding", "embed_content_async", "AsyncEmbedContent"),
    ("google.generativeai", "embed_content", "EmbedContent"),
    ("google.generativeai", "embed_content_async", "AsyncEmbedContent"),
)


def _resolve(module_name: str, dotted: str) -> Tuple[Any, str]:
    import importlib

    owner: Any = importlib.import_module(module_name)
    *path, attribute = dotted.split(".")
    for name in path:
        owner = getattr(owner, name)
    return owner, attribute


class GoogleGenerativeAIInstrumentor(BaseInstrumentor):  # type: ignore
    """An instrumentor for `google-generativeai`."""

    _originals: Dict[Tuple[str, str], Any]

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = get_tracer_provider()
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)
        tracer = FITracer(get_tracer(__name__, __version__, tracer_provider), config=config)
        try:
            import google.generativeai  # noqa: F401
        except ImportError as err:
            raise Exception(
                "Could not import google-generativeai. Please install with `pip install google-generativeai`."
            ) from err

        from traceai_google_generativeai import _wrappers

        self._originals = {}
        for module_name, dotted, wrapper_name in _TARGETS:
            owner, attribute = _resolve(module_name, dotted)
            self._originals[(module_name, dotted)] = owner.__dict__.get(attribute, getattr(owner, attribute))
            wrap_function_wrapper(module_name, dotted, getattr(_wrappers, wrapper_name)(tracer))

    def _uninstrument(self, **kwargs: Any) -> None:
        for (module_name, dotted), original in getattr(self, "_originals", {}).items():
            owner, attribute = _resolve(module_name, dotted)
            setattr(owner, attribute, original)
        self._originals = {}
