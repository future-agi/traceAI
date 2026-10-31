import sys
from types import ModuleType, SimpleNamespace

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_transformers import TransformersInstrumentor


class Tokenizer:
    def encode(self, value, add_special_tokens=True):
        return list(range(len(value.split()) + int(add_special_tokens)))

    def batch_decode(self, rows, **kwargs):
        return [" ".join({1: "hello", 2: "world", 3: "new", 4: "tokens"}.get(i, "")
                         for i in row).strip() for row in rows]


@pytest.fixture
def fake_modules(monkeypatch):
    class GenerationMixin:
        def generate(self, inputs=None, generation_config=None, **kwargs):
            self.calls.append((inputs, kwargs))
            if self.error is not None:
                raise self.error
            streamer = kwargs.get("streamer")
            if streamer is not None:
                streamer.put(inputs)
                streamer.put([[3]])
                streamer.put([[4]])
            return self.result

    class Model(GenerationMixin):
        def __init__(self):
            self.config = SimpleNamespace(_name_or_path="tiny-local", is_encoder_decoder=False)
            self.generation_config = SimpleNamespace(max_new_tokens=2, temperature=1.0, eos_token_id=99)
            self.result = [[1, 2, 3, 4]]
            self.calls = []
            self.error = None

    class Pipeline:
        task = "text-generation"

        def __init__(self, task=None):
            self.model = Model()
            self.tokenizer = Tokenizer()
            self.feature_extractor = SimpleNamespace(sampling_rate=16000)
            self.result = [{"generated_text": "hello world new tokens"}]
            self.seen = []
            self._forward_params = {}
            if task:
                self.task = task

        def __call__(self, inputs=None, **kwargs):
            self.seen.append((inputs, kwargs))
            if self.task not in ("feature-extraction", "automatic-speech-recognition"):
                self.model.generate([[1, 2]], **kwargs)
            if self.task == "automatic-speech-recognition" and self.model.error is not None:
                raise self.model.error
            return self.result

    class TextGenerationPipeline(Pipeline):
        def __call__(self, text_inputs=None, **kwargs):
            return super().__call__(text_inputs, **kwargs)

    class Text2TextGenerationPipeline(Pipeline):
        task = "text2text-generation"

        def __call__(self, *args, **kwargs):
            return super().__call__(*args, **kwargs)

    class FeatureExtractionPipeline(Pipeline):
        task = "feature-extraction"

        def __call__(self, *args, **kwargs):
            return super().__call__(*args, **kwargs)

    class AutomaticSpeechRecognitionPipeline(Pipeline):
        task = "automatic-speech-recognition"

        def __call__(self, *args, **kwargs):
            return super().__call__(*args, **kwargs)

    class TextStreamer:
        def __init__(self):
            self.received = []
            self.result = object()

        def put(self, value):
            self.received.append(value)
            return self.result

    targets = {
        "transformers.pipelines.base": Pipeline,
        "transformers.pipelines.text_generation": TextGenerationPipeline,
        "transformers.pipelines.text2text_generation": Text2TextGenerationPipeline,
        "transformers.pipelines.feature_extraction": FeatureExtractionPipeline,
        "transformers.pipelines.automatic_speech_recognition": AutomaticSpeechRecognitionPipeline,
        "transformers.generation.utils": GenerationMixin,
        "transformers.generation.streamers": TextStreamer,
    }
    for name, cls in targets.items():
        module = ModuleType(name)
        setattr(module, cls.__name__, cls)
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(Model=Model, Pipeline=Pipeline, TextGenerationPipeline=TextGenerationPipeline,
                           Text2TextGenerationPipeline=Text2TextGenerationPipeline,
                           FeatureExtractionPipeline=FeatureExtractionPipeline,
                           AutomaticSpeechRecognitionPipeline=AutomaticSpeechRecognitionPipeline,
                           TextStreamer=TextStreamer, GenerationMixin=GenerationMixin)


@pytest.fixture
def tracing(fake_modules):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = TransformersInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    yield SimpleNamespace(instrumentor=instrumentor, exporter=exporter, provider=provider,
                          fakes=fake_modules, config=TraceConfig)
    instrumentor.uninstrument()
    provider.shutdown()
