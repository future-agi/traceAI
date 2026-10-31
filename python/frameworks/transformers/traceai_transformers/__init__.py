"""Instrumentation for local Transformers pipelines and PyTorch generation."""

import logging
from importlib import import_module
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import FunctionWrapper

from traceai_transformers._wrappers import (
    PRIVACY_AVAILABLE,
    GenerateWrapper,
    PipelineWrapper,
    StreamerWrapper,
)
from traceai_transformers.package import _instruments
from traceai_transformers.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class TransformersInstrumentor(BaseInstrumentor):
    """Trace eager local inference without downloading models or changing results.

    ``tokenizer=`` optionally supplies a tokenizer for decoding direct ``generate``
    calls. Pipeline tokenizers are discovered automatically. Installation of an
    inference backend (for example CPU PyTorch) remains the application's choice.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        self._patches = []
        self._enabled = False
        self._epoch = object()
        if not PRIVACY_AVAILABLE:
            logger.warning("Transformers instrumentation requires core privacy support; inference is unchanged")
            return
        try:
            config = kwargs.get("config")
            if not isinstance(config, TraceConfig):
                config = TraceConfig()
            self._tracer = FITracer(
                trace_api.get_tracer(
                    __name__, __version__, kwargs.get("tracer_provider")
                ),
                config=config,
            )
            pipeline = PipelineWrapper(self, config)
            generate = GenerateWrapper(self, config, kwargs.get("tokenizer"))
            # Patch overrides as well as the base: TextGenerationPipeline converts
            # message lists to private Chat objects before delegating to Pipeline.
            targets = (
                ("transformers.pipelines.base", "Pipeline", "__call__", pipeline),
                ("transformers.pipelines.text_generation", "TextGenerationPipeline", "__call__", pipeline),
                ("transformers.pipelines.text2text_generation", "Text2TextGenerationPipeline", "__call__", pipeline),
                ("transformers.pipelines.feature_extraction", "FeatureExtractionPipeline", "__call__", pipeline),
                ("transformers.pipelines.automatic_speech_recognition", "AutomaticSpeechRecognitionPipeline", "__call__", pipeline),
                ("transformers.generation.utils", "GenerationMixin", "generate", generate),
                ("transformers.generation.streamers", "TextStreamer", "put", StreamerWrapper(self)),
            )
            for module, class_name, name, wrapper in targets:
                try:
                    cls = getattr(import_module(module), class_name)
                    # Do not install redundant wrappers on inherited methods.
                    if name not in vars(cls):
                        continue
                    original = getattr(cls, name)
                    replacement = FunctionWrapper(original, wrapper)
                    setattr(cls, name, replacement)
                    self._patches.append((cls, name, original, replacement))
                except Exception:
                    logger.debug("Transformers hook unavailable: %s.%s", class_name, name)
            self._enabled = bool(self._patches)
            if not self._enabled:
                logger.warning("No supported Transformers inference hooks are available")
        except Exception:
            self._uninstrument()
            logger.warning("Transformers instrumentation unavailable; inference is unchanged")

    def _uninstrument(self, **kwargs: Any) -> None:
        self._enabled = False
        for cls, name, original, replacement in reversed(getattr(self, "_patches", ())):
            try:
                # Do not overwrite a wrapper installed by someone else later. An
                # embedded copy of our wrapper becomes an inert pass-through.
                if vars(cls).get(name) is replacement:
                    setattr(cls, name, original)
            except Exception:
                logger.debug("Could not restore Transformers hook %s", name)
        self._patches = []


__all__ = ["TransformersInstrumentor", "__version__"]
