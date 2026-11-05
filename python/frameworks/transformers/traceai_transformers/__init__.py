"""Trace local Transformers pipelines, independently of remote Hub clients."""
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import FunctionWrapper

from ._wrappers import _PipelineWrapper
from .version import __version__

# Look up optional historical classes individually: Transformers 5 removed them.
_TARGETS = (
    ("TextGenerationPipeline", "text-generation", "LLM"),
    ("FeatureExtractionPipeline", "feature-extraction", "EMBEDDING"),
    ("Text2TextGenerationPipeline", "text2text-generation", "LLM"),
    ("SummarizationPipeline", "summarization", "LLM"),
    ("TranslationPipeline", "translation", "LLM"),
    ("QuestionAnsweringPipeline", "question-answering", "CHAIN"),
)


class TransformersInstrumentor(BaseInstrumentor):
    """Instrument supported pipeline calls; bare ``model.generate`` is not patched.

    ``capture_content=False`` (default) records metadata only. Opt in to bounded
    input/output previews with ``capture_content=True``; ``config=TraceConfig``
    still controls privacy. Embedding vectors and lazy result payloads are never
    collected. Supply ``tracer_provider`` to avoid changing the global provider.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("transformers>=4.42,<6",)

    def _instrument(self, **kwargs: Any) -> None:
        import transformers.pipelines as pipelines

        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        capture = kwargs.get("capture_content", False)
        if not isinstance(capture, bool):
            raise TypeError("capture_content must be a bool")
        tracer = FITracer(
            trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")),
            config=config,
        )
        self._patches = []
        try:
            for name, task, kind in _TARGETS:
                cls = getattr(pipelines, name, None)
                if cls is None:
                    continue
                own_method = "__call__" in vars(cls)
                original = vars(cls)["__call__"] if own_method else cls.__call__
                wrapper = _PipelineWrapper(tracer=tracer, config=config, capture=capture, task=task, kind=kind)
                patched = FunctionWrapper(original, wrapper)
                self._patches.append((cls, own_method, original, patched, wrapper))
                cls.__call__ = patched
        except BaseException:
            self._uninstrument()
            raise

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, own_method, original, patched, wrapper in reversed(self._patches):
            # An outer, third-party patch may still hold ours: disable it without
            # overwriting their patch. In-flight lazy iterators finish normally.
            wrapper.enabled = False
            if vars(cls).get("__call__") is patched:
                if own_method:
                    cls.__call__ = original
                else:
                    delattr(cls, "__call__")
        self._patches = []


__all__ = ["TransformersInstrumentor", "__version__"]
