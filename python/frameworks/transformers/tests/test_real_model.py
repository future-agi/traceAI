"""Opt-in, actual CPU inference. No provider keys or collector are needed."""

import json
import os
import weakref
from collections import OrderedDict, UserDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_transformers import TransformersInstrumentor, _wrappers


class ChatText(str):
    pass

pytestmark = pytest.mark.skipif(os.getenv("TRACEAI_TRANSFORMERS_REAL_TEST") != "1",
                                reason="set TRACEAI_TRANSFORMERS_REAL_TEST=1 for real CPU model tests")
ROOT = Path(__file__).resolve().parents[1]
MODEL = "hf-internal-testing/tiny-random-gpt2"
REVISION = "71034c5d8bde858ff824298bdedc65515b97d2b9"


@pytest.fixture(scope="module")
def real_model():
    # Cache defaults cannot land on the laptop's root disk, even when the caller
    # forgot to set HF_HOME. Never import/load remote Python or pickle weights.
    os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    torch.set_num_threads(2)
    tokenizer = AutoTokenizer.from_pretrained(MODEL, revision=REVISION, cache_dir=str(ROOT / ".cache" / "hub"), trust_remote_code=False, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(MODEL, revision=REVISION, cache_dir=str(ROOT / ".cache" / "hub"),
                                                trust_remote_code=False, local_files_only=True, use_safetensors=True).to("cpu").eval()
    assert not torch.cuda.is_available()
    yield model, tokenizer


@pytest.fixture
def real_tracing(real_model):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = TransformersInstrumentor()
    yield instrumentor, exporter, provider
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


def test_real_tiny_gpt2_generate_baseline_parity_and_tokens(real_model, real_tracing):
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    inputs = tokenizer("Hello local world", return_tensors="pt")
    baseline = model.generate(**inputs, max_new_tokens=3, do_sample=False, return_dict_in_generate=True)
    instrumentor.instrument(tracer_provider=provider, tokenizer=tokenizer)
    result = model.generate(**inputs, max_new_tokens=3, do_sample=False, return_dict_in_generate=True)
    assert type(result) is type(baseline)
    assert result.sequences.equal(baseline.sequences)
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.usage.input_tokens"] == inputs["input_ids"].shape[-1]
    assert span.attributes["gen_ai.usage.output_tokens"] == 3
    assert "gen_ai.output.messages.0.message.content" in span.attributes
    instrumentor.uninstrument()
    assert model.generate(**inputs, max_new_tokens=3, do_sample=False).equal(result.sequences)
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("decoder_prompt", [[42, 43], [0, 42, 43]],
                         ids=["prepended-start", "existing-start"])
def test_real_tiny_t5_hidden_decoder_prompt_and_output_usage(real_tracing, decoder_prompt):
    import torch
    from transformers import T5Config, T5ForConditionalGeneration
    instrumentor, exporter, provider = real_tracing
    model = T5ForConditionalGeneration(T5Config(
        vocab_size=64, d_model=16, d_kv=8, d_ff=32, num_layers=1,
        num_decoder_layers=1, num_heads=2, decoder_start_token_id=0,
        pad_token_id=0, eos_token_id=1,
    )).to("cpu").eval()

    class DecoderTokenizer:
        def batch_decode(self, rows, **kwargs):
            vocabulary = {0: "", 7: "token7", 42: "PRIVATE_DECODER_42", 43: "PRIVATE_DECODER_43"}
            return [" ".join(vocabulary.get(token, "encoder") for token in row).strip() for row in rows]

    inputs = torch.tensor([[5, 6]])
    decoder_ids = torch.tensor([decoder_prompt])
    kwargs = dict(decoder_input_ids=decoder_ids, max_new_tokens=2, do_sample=False,
                  prefix_allowed_tokens_fn=lambda batch_id, input_ids: [7], return_dict_in_generate=True)
    baseline = model.generate(inputs, **kwargs)
    assert baseline.sequences.tolist() == [[0, 42, 43, 7, 7]]
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True),
                           tokenizer=DecoderTokenizer())
    result = model.generate(inputs, **kwargs)
    assert type(result) is type(baseline)
    assert result.sequences.equal(baseline.sequences)
    assert decoder_ids.tolist() == [decoder_prompt]
    span, = exporter.get_finished_spans()
    attrs = span.attributes
    assert attrs["gen_ai.output.messages.0.message.content"] == "token7 token7"
    assert attrs["gen_ai.usage.input_tokens"] == 2
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.total_tokens"] == 4
    assert "PRIVATE_DECODER" not in repr(attrs)


def test_real_text_generation_pipeline_one_span_and_chat(real_model, real_tracing):
    from transformers import pipeline
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    generator = pipeline("text-generation", model=model, tokenizer=tokenizer, device=-1)
    baseline = generator("Hello", max_new_tokens=2, do_sample=False)
    instrumentor.instrument(tracer_provider=provider)
    assert generator("Hello", max_new_tokens=2, do_sample=False) == baseline
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.input.messages.0.message.content"] == "Hello"
    assert span.attributes["gen_ai.usage.output_tokens"] == 2
    exporter.clear()
    old_template = tokenizer.chat_template
    try:
        tokenizer.chat_template = "{% for message in messages %}{{message['role'] + ': ' + message['content'] + '\\n'}}{% endfor %}assistant: "
        chat = [{"role": "user", "content": "Hello"}]
        response = generator(chat, max_new_tokens=2, do_sample=False)
        assert response[0]["generated_text"][0] == chat[0]
        chat_span, = exporter.get_finished_spans()
        assert chat_span.attributes["gen_ai.input.messages.0.message.role"] == "user"
        assert chat_span.attributes["gen_ai.input.messages.0.message.content"] == "Hello"
        assert "gen_ai.output.messages.0.message.content" in chat_span.attributes
        assert chat_span.attributes["gen_ai.usage.output_tokens"] == 2
    finally:
        tokenizer.chat_template = old_template


@pytest.mark.parametrize("message_type", [dict, OrderedDict, UserDict])
@pytest.mark.parametrize("content_type", [str, ChatText])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text", "hide_input_messages"])
@pytest.mark.parametrize("configuration", ["call", "pipeline-default"])
def test_real_chat_user_continuation_hidden_inputs_and_parity(
        real_model, real_tracing, flag, configuration, message_type, content_type):
    from transformers import pipeline
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    # HF dispatch requires a dict-compatible first message, but subsequent chat
    # messages accept generic Mappings such as UserDict.
    first_type = dict if message_type is UserDict else message_type
    source = [first_type(role="system", content=content_type("PRIVATE_SYSTEM")),
              message_type(role="user", content=content_type("PRIVATE_USER_PREFIX"))]
    original_source = [dict(message) for message in source]
    old_template = tokenizer.chat_template
    try:
        tokenizer.chat_template = (
            "{% for message in messages %}{{message['role'] + ': ' + message['content'] + '\\n'}}{% endfor %}"
            "{% if add_generation_prompt %}assistant: {% endif %}"
        )
        continuation = {"continue_final_message": True}
        generator = pipeline("text-generation", model=model, tokenizer=tokenizer, device=-1,
                             **(continuation if configuration == "pipeline-default" else {}))
        kwargs = {"max_new_tokens": 2, "do_sample": False}
        if configuration == "call":
            kwargs.update(continuation)
        baseline = generator(source, **kwargs)
        expected_suffix = baseline[0]["generated_text"][-1]["content"][len(source[-1]["content"]):]
        assert expected_suffix
        assert not exporter.get_finished_spans()
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**{flag: True}))
        result = generator(source, **kwargs)
        assert type(result) is type(baseline) and result == baseline
        assert source == original_source
        span, = exporter.get_finished_spans()
        attrs = span.attributes
        assert attrs["gen_ai.output.messages.0.message.role"] == "user"
        assert attrs["gen_ai.output.messages.0.message.content"] == expected_suffix
        assert json.loads(attrs["output.value"]) == [{"role": "user", "content": expected_suffix}]
        assert attrs["gen_ai.usage.output_tokens"] == 2
        assert "gen_ai.output.messages.1.message.content" not in attrs
        assert "PRIVATE_SYSTEM" not in repr(attrs)
        assert "PRIVATE_USER_PREFIX" not in repr(attrs)
    finally:
        tokenizer.chat_template = old_template


def test_real_text_iterator_streamer_thread_and_ttft(real_model, real_tracing):
    from transformers import TextIteratorStreamer
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    inputs = tokenizer("Hello", return_tensors="pt")
    baseline = model.generate(**inputs, max_new_tokens=3, do_sample=False)
    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, timeout=20, skip_special_tokens=True)
    instrumentor.instrument(tracer_provider=provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(model.generate, **inputs, streamer=streamer, max_new_tokens=3, do_sample=False)
        output = "".join(streamer)
        result = future.result(timeout=20)
    assert result.equal(baseline)
    assert output == tokenizer.decode(baseline[0, inputs["input_ids"].shape[-1]:], skip_special_tokens=True)
    span, = exporter.get_finished_spans()
    assert 0 <= span.attributes["gen_ai.response.time_to_first_token"] <= span.attributes["gen_ai.response.latency"]
    assert span.attributes["gen_ai.usage.output_tokens"] == 3


@pytest.mark.parametrize("failure", ["tracer", "processor"])
@pytest.mark.parametrize("invocation", ["direct", "pipeline"])
@pytest.mark.parametrize("streaming", ["none", "text", "iterator"])
def test_real_span_creation_failure_preserves_generation_and_streaming(
        real_model, real_tracing, monkeypatch, capsys, failure, invocation, streaming):
    from transformers import TextIteratorStreamer, TextStreamer, pipeline
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    inputs = tokenizer("Hello", return_tensors="pt")
    generator = pipeline("text-generation", model=model, tokenizer=tokenizer, device=-1)
    streamers = []
    states = []

    class RecordingStreamer(TextIteratorStreamer if streaming == "iterator" else TextStreamer):
        def __init__(self):
            super().__init__(tokenizer, skip_prompt=True, skip_special_tokens=True)
            self.received = []

        def put(self, value):
            self.received.append(value.clone())
            states.append((_wrappers._ACTIVE.get(), _wrappers._STREAM.get(), _wrappers._GENERATING.get()))
            return super().put(value)

    def generate():
        kwargs = {"max_new_tokens": 2, "do_sample": False}
        if streaming != "none":
            streamer = RecordingStreamer()
            streamers.append(streamer)
            kwargs["streamer"] = streamer
        result = generator("Hello", **kwargs) if invocation == "pipeline" else model.generate(**inputs, **kwargs)
        text = "".join(streamers[-1]) if streaming == "iterator" else capsys.readouterr().out
        return result, text

    baseline, baseline_text = generate()
    assert not exporter.get_finished_spans()

    class BrokenProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            raise RuntimeError("telemetry unavailable")

    class BrokenTracer:
        def start_span(self, *args, **kwargs):
            raise RuntimeError("telemetry unavailable")

    if failure == "processor":
        provider.add_span_processor(BrokenProcessor())
    instrumentor.instrument(tracer_provider=provider)
    if failure == "tracer":
        monkeypatch.setattr(instrumentor, "_tracer", BrokenTracer())
    parent = trace_api.get_current_span()
    result, result_text = generate()
    assert type(result) is type(baseline)
    assert (result == baseline) if invocation == "pipeline" else result.equal(baseline)
    assert result_text == baseline_text
    if streaming != "none":
        assert len(streamers[0].received) == len(streamers[1].received) == 3
        assert all(left.equal(right) for left, right in zip(streamers[0].received, streamers[1].received))
        assert states and all(state == (None, None, False) for state in states)
    assert not exporter.get_finished_spans()
    assert trace_api.get_current_span() is parent
    assert _wrappers._ACTIVE.get() is None and _wrappers._STREAM.get() is None
    assert not _wrappers._GENERATING.get()


def test_real_feature_extraction_pipeline_no_vectors(real_model, real_tracing):
    from transformers import AutoModel, pipeline
    _, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    encoder = AutoModel.from_pretrained(MODEL, revision=REVISION, cache_dir=str(ROOT / ".cache" / "hub"),
                                       trust_remote_code=False, local_files_only=True, use_safetensors=True).to("cpu").eval()
    embedder = pipeline("feature-extraction", model=encoder, tokenizer=tokenizer, device=-1)
    baseline = embedder("Hello")
    instrumentor.instrument(tracer_provider=provider)
    result = embedder("Hello")
    assert result == baseline
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.span.kind"] == "EMBEDDING"
    assert span.attributes["gen_ai.embeddings.dimension.count"] == encoder.config.hidden_size
    assert "output.value" not in span.attributes
    assert not any("vector" in key for key in span.attributes)


@pytest.mark.parametrize("audio_key", ["array", "raw"])
def test_real_wav2vec2_asr_dictionary_duration_and_parity(real_tracing, tmp_path, audio_key):
    import numpy as np
    from transformers import (
        Wav2Vec2Config,
        Wav2Vec2CTCTokenizer,
        Wav2Vec2FeatureExtractor,
        Wav2Vec2ForCTC,
        pipeline,
    )

    instrumentor, exporter, provider = real_tracing
    vocabulary = {"<pad>": 0, "<s>": 1, "</s>": 2, "<unk>": 3, "|": 4, "a": 5, "b": 6}
    vocab_file = tmp_path / "vocab.json"
    vocab_file.write_text(json.dumps(vocabulary))
    tokenizer = Wav2Vec2CTCTokenizer(str(vocab_file))
    model = Wav2Vec2ForCTC(Wav2Vec2Config(
        vocab_size=len(vocabulary), hidden_size=8, num_hidden_layers=1,
        num_attention_heads=2, intermediate_size=16, conv_dim=(8, 8, 8),
        conv_stride=(5, 2, 2), conv_kernel=(10, 3, 3),
        num_conv_pos_embedding_groups=2, num_conv_pos_embeddings=8,
        mask_time_prob=0.0,
    )).to("cpu").eval()
    feature_extractor = Wav2Vec2FeatureExtractor(sampling_rate=16000)
    transcriber = pipeline("automatic-speech-recognition", model=model, tokenizer=tokenizer,
                           feature_extractor=feature_extractor, device=-1)
    samples = np.zeros(1600, dtype=np.float32)
    baseline_source = {audio_key: samples.copy(), "sampling_rate": 16000}
    baseline = transcriber(baseline_source)
    assert baseline_source == {}
    instrumentor.instrument(tracer_provider=provider)
    source = {audio_key: samples, "sampling_rate": 16000}
    result = transcriber(source)
    assert result == baseline and source == {}
    assert samples.shape == (1600,) and not samples.any()
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.audio.duration_secs"] == 0.1
    assert span.attributes["gen_ai.operation.name"] == "speech_to_text"
    assert "input.value" not in span.attributes
    assert not any(key.startswith("gen_ai.input.") for key in span.attributes)
    if result["text"]:
        assert span.attributes["gen_ai.audio.transcript"] == result["text"]


def test_real_pipeline_effective_generation_configuration(real_model, real_tracing):
    from transformers import pipeline
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    generator = pipeline("text-generation", model=model, tokenizer=tokenizer, device=-1)
    generator.generation_config.max_new_tokens = 3
    baseline = generator("Hello", do_sample=False)
    instrumentor.instrument(tracer_provider=provider)
    assert generator("Hello", do_sample=False) == baseline
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.usage.output_tokens"] == 3
    assert span.attributes["gen_ai.request.max_tokens"] == 3
    assert json.loads(span.attributes["gen_ai.request.parameters"])["max_new_tokens"] == 3


def test_real_embedding_only_generation_output_usage(real_model, real_tracing):
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    inputs = tokenizer("Hello local world", return_tensors="pt")
    embeddings = model.get_input_embeddings()(inputs["input_ids"])
    kwargs = dict(inputs_embeds=embeddings, attention_mask=inputs["attention_mask"],
                  max_new_tokens=3, do_sample=False, return_dict_in_generate=True)
    baseline = model.generate(**kwargs)
    assert baseline.sequences.shape[-1] == 3
    instrumentor.instrument(tracer_provider=provider, tokenizer=tokenizer)
    result = model.generate(**kwargs)
    assert type(result) is type(baseline) and result.sequences.equal(baseline.sequences)
    span, = exporter.get_finished_spans()
    attrs = span.attributes
    assert attrs["gen_ai.usage.input_tokens"] == int(inputs["attention_mask"].sum())
    assert attrs["gen_ai.usage.output_tokens"] == 3
    assert attrs["gen_ai.usage.total_tokens"] == int(inputs["attention_mask"].sum()) + 3
    assert attrs["gen_ai.output.messages.0.message.content"] == tokenizer.decode(
        baseline.sequences[0], skip_special_tokens=True)


def test_real_pipeline_batches_do_not_retain_completed_kv_caches(real_model, real_tracing, monkeypatch):
    from transformers import pipeline
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    generator = pipeline("text-generation", model=model, tokenizer=tokenizer, device=-1)

    def run():
        generate = model.generate
        caches = []
        live_caches_at_entry = []

        def observed(*args, **kwargs):
            live_caches_at_entry.append(sum(reference() is not None for reference in caches))
            result = generate(*args, **kwargs)
            assert result.past_key_values is not None
            caches.append(weakref.ref(result.past_key_values))
            return result

        with monkeypatch.context() as patch:
            patch.setattr(model, "generate", observed)
            result = generator(["Hello"] * 5, batch_size=1, max_new_tokens=2,
                               do_sample=False, return_dict_in_generate=True)
        assert all(reference() is None for reference in caches)
        return result, live_caches_at_entry

    baseline, baseline_caches = run()
    instrumentor.instrument(tracer_provider=provider)
    result, traced_caches = run()
    assert result == baseline
    assert traced_caches == baseline_caches == [0, 0, 0, 0, 0]
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.usage.output_tokens"] == 10


def test_real_batch_preserves_essential_metadata_at_default_attribute_limit(real_model, real_tracing):
    from transformers import pipeline
    model, tokenizer = real_model
    instrumentor, exporter, provider = real_tracing
    generator = pipeline("text-generation", model=model, tokenizer=tokenizer, device=-1)
    source = ["Hello"] * 32
    baseline = generator(source, batch_size=1, max_new_tokens=1, do_sample=False)
    instrumentor.instrument(tracer_provider=provider)
    assert generator(source, batch_size=1, max_new_tokens=1, do_sample=False) == baseline
    span, = exporter.get_finished_spans()
    attrs = span.attributes
    for key in ("gen_ai.span.kind", "gen_ai.operation.name", "gen_ai.provider.name",
                "traceai.capture.layer", "gen_ai.request.model", "gen_ai.response.model",
                "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.response.latency"):
        assert key in attrs
    assert attrs["gen_ai.usage.output_tokens"] == 32
