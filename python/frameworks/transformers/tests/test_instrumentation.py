import json
import time
from collections import OrderedDict, UserDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fi_instrumentation import TraceConfig, register_secret
from fi_instrumentation.instrumentation.config import suppress_tracing
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import StatusCode

from traceai_transformers import TransformersInstrumentor
from traceai_transformers import _attributes as extract
from traceai_transformers import _wrappers
from traceai_transformers.package import _instruments


def spans(tracing):
    return tracing.exporter.get_finished_spans()


def reconfigure(tracing, **config):
    tracing.instrumentor.uninstrument()
    tracing.exporter.clear()
    tracing.instrumentor.instrument(tracer_provider=tracing.provider, config=TraceConfig(**config))


def test_package_dependency():
    assert TransformersInstrumentor().instrumentation_dependencies() == _instruments


def test_text_generation_identity_usage_and_no_nested_duplicates(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    source = "hello world"
    result = pipe(source, max_new_tokens=2, temperature=0.25)
    assert result is pipe.result
    assert pipe.seen[0][0] is source
    assert len(pipe.model.calls) == 1
    assert len(spans(tracing)) == 1
    span = spans(tracing)[0]
    attrs = span.attributes
    assert span.kind is trace_api.SpanKind.CLIENT
    assert span.status.status_code is StatusCode.OK
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.request.model"] == "tiny-local"
    assert attrs["gen_ai.input.messages.0.message.content"] == source
    assert attrs["gen_ai.output.messages.0.message.content"] == " new tokens"
    assert attrs["gen_ai.usage.input_tokens"] == 2
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.total_tokens"] == 4
    assert attrs["gen_ai.request.temperature"] == 0.25
    assert json.loads(attrs["gen_ai.request.parameters"])["max_new_tokens"] == 2
    assert attrs["gen_ai.response.latency"] == (span.end_time - span.start_time) / 1e9


def test_chat_template_messages_and_multimodal_no_media(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    source = [{"role": "system", "content": "brief"}, {"role": "user", "content": [
        {"type": "text", "text": "hello"}, {"type": "image", "url": "secret-image"},
        {"type": "audio", "data": b"raw-media"}]}]
    pipe.result = [{"generated_text": source + [{"role": "assistant", "content": "world"}]}]
    assert pipe(source) is pipe.result
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.input.messages.0.message.role"] == "system"
    assert attrs["gen_ai.input.messages.1.message.content"] == "hello"
    assert attrs["gen_ai.output.messages.0.message.content"] == "world"
    assert "raw-media" not in repr(attrs) and "secret-image" not in repr(attrs)


@pytest.mark.parametrize("task,key", [("text2text-generation", "generated_text"),
    ("summarization", "summary_text"), ("translation_en_to_de", "translation_text")])
def test_text2text_and_alias_tasks(tracing, task, key):
    pipe = tracing.fakes.Text2TextGenerationPipeline(task)
    pipe.result = [{key: "hello world translated"}]
    assert pipe("hello world") is pipe.result
    assert spans(tracing)[0].attributes["gen_ai.output.messages.0.message.content"] == "hello world translated"


def test_return_full_text_false_does_not_strip_generated_prompt(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    pipe.result = [{"generated_text": "hello world repeats"}]
    pipe("hello world", return_full_text=False)
    assert spans(tracing)[0].attributes["gen_ai.output.messages.0.message.content"] == "hello world repeats"


def test_pipeline_default_new_text_does_not_strip_repeated_prompt(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    pipe._postprocess_params = {"return_type": SimpleNamespace(name="NEW_TEXT")}
    pipe.result = [{"generated_text": "hello world repeats"}]
    pipe("hello world")
    assert spans(tracing)[0].attributes["gen_ai.output.messages.0.message.content"] == "hello world repeats"


class ChatText(str):
    pass


@pytest.mark.parametrize("message_type", [dict, OrderedDict, UserDict])
@pytest.mark.parametrize("content_type", [str, ChatText])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text", "hide_input_messages"])
@pytest.mark.parametrize("final_role", ["assistant", "user", "system", "tool"])
@pytest.mark.parametrize("configuration", ["call", "pipeline-default"])
def test_chat_continuation_does_not_export_input_as_output(
        tracing, flag, final_role, configuration, message_type, content_type):
    reconfigure(tracing, **{flag: True})
    pipe = tracing.fakes.TextGenerationPipeline()
    source = [message_type(role="system", content=content_type("PRIVATE_SYSTEM")),
              message_type(role=final_role, content=content_type("PRIVATE_FINAL_PREFIX"))]
    original_source = [dict(message) for message in source]
    pipe.result = [{"generated_text": [source[0],
                   {"role": final_role, "content": "PRIVATE_FINAL_PREFIX public continuation"}]}]
    kwargs = {"continue_final_message": True}
    if configuration == "pipeline-default":
        pipe._preprocess_params = dict(kwargs)
        pipe._postprocess_params = dict(kwargs)
        kwargs = {}
    assert pipe(source, **kwargs) is pipe.result
    assert pipe.seen[0][0] is source and len(pipe.seen) == 1
    assert source == original_source
    assert pipe.result[0]["generated_text"][-1]["content"] == "PRIVATE_FINAL_PREFIX public continuation"
    span, = spans(tracing)
    attrs = span.attributes
    assert attrs["gen_ai.output.messages.0.message.role"] == final_role
    assert attrs["gen_ai.output.messages.0.message.content"] == " public continuation"
    assert json.loads(attrs["output.value"]) == [{"role": final_role, "content": " public continuation"}]
    assert "gen_ai.output.messages.1.message.content" not in attrs
    assert "PRIVATE_SYSTEM" not in repr(attrs)
    assert "PRIVATE_FINAL_PREFIX" not in repr(attrs)


def test_batched_chat_continuations_preserve_only_generated_suffixes():
    prompts = [[{"role": "system", "content": "PRIVATE_SYSTEM"},
                {"role": role, "content": "PRIVATE_PREFIX"}] for role in ("user", "tool")]
    result = [[{"generated_text": [prompt[0], {"role": prompt[-1]["role"],
                                             "content": "PRIVATE_PREFIX reply " + str(index)}]}]
              for index, prompt in enumerate(prompts)]
    assert extract.output_messages(result, prompts, {"continue_final_message": True}) == [
        {"role": "user", "content": " reply 0"}, {"role": "tool", "content": " reply 1"}]


def test_embedding_vectors_never_recorded(tracing):
    pipe = tracing.fakes.FeatureExtractionPipeline()
    pipe.result = [[[0.1234, 0.5678, 0.9999]]]
    assert pipe(["hello", "two words"]) is pipe.result
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.span.kind"] == "EMBEDDING"
    assert attrs["gen_ai.embeddings.dimension.count"] == 3
    assert attrs["gen_ai.request.embedding_inputs_count"] == 2
    assert attrs["gen_ai.usage.input_tokens"] == 5
    assert "0.1234" not in repr(attrs) and "output.value" not in attrs


def test_asr_transcript_duration_never_audio(tracing):
    pipe = tracing.fakes.AutomaticSpeechRecognitionPipeline()
    pipe.result = {"text": "heard words", "chunks": [{"audio": b"raw-media"}]}
    source = {"array": [0.75] * 1600, "sampling_rate": 16000, "path": "private.wav"}
    assert pipe(source) is pipe.result
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.operation.name"] == "speech_to_text"
    assert attrs["gen_ai.audio.transcript"] == "heard words"
    assert attrs["gen_ai.audio.duration_secs"] == 0.1
    assert "input.value" not in attrs
    assert "raw-media" not in repr(attrs) and "private.wav" not in repr(attrs)
    assert "0.75" not in repr(attrs)


@pytest.mark.parametrize("audio_key", ["array", "raw"])
@pytest.mark.parametrize("invocation", ["positional", "keyword"])
def test_asr_duration_is_snapshotted_before_destructive_preprocessing(tracing, audio_key, invocation):
    pipe = tracing.fakes.AutomaticSpeechRecognitionPipeline()
    pipe.result = {"text": "heard words"}
    source = {audio_key: [0.75] * 1600, "sampling_rate": 16000}
    calls = []

    def preprocess(*args, **kwargs):
        actual = args[0] if args else kwargs["inputs"]
        assert actual is source
        calls.append(actual)
        actual.pop(audio_key)
        actual.pop("sampling_rate")
        return pipe.result

    wrapper = _wrappers.PipelineWrapper(tracing.instrumentor, TraceConfig())
    args, kwargs = ((source,), {}) if invocation == "positional" else ((), {"inputs": source})
    assert wrapper(preprocess, pipe, args, kwargs) is pipe.result
    assert len(calls) == 1 and source == {}
    span, = spans(tracing)
    assert span.attributes["gen_ai.audio.duration_secs"] == 0.1
    assert span.attributes["gen_ai.audio.transcript"] == "heard words"
    assert "0.75" not in repr(span.attributes)


@pytest.mark.parametrize("config", [{"hide_outputs": True}, {"hide_output_text": True}, {"hide_output_messages": True}])
def test_asr_hide_flags_cover_transcript_alias(tracing, config):
    reconfigure(tracing, **config)
    pipe = tracing.fakes.AutomaticSpeechRecognitionPipeline()
    pipe.result = {"text": "sensitive transcript"}
    pipe(b"raw-audio")
    assert "sensitive transcript" not in repr(spans(tracing)[0].attributes)


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text", "hide_input_messages"])
def test_input_hide_flags_cover_aggregates(tracing, flag):
    reconfigure(tracing, **{flag: True})
    tracing.fakes.TextGenerationPipeline()("sensitive input")
    attrs = spans(tracing)[0].attributes
    assert "sensitive input" not in repr(attrs)


def test_hidden_generation_parameters_have_no_aliases(tracing):
    reconfigure(tracing, hide_llm_invocation_parameters=True)
    tracing.fakes.TextGenerationPipeline()("hello world", temperature=0.125)
    attrs = spans(tracing)[0].attributes
    assert "gen_ai.request.parameters" not in attrs
    assert "gen_ai.request.temperature" not in attrs


def test_credentials_signed_urls_and_inline_media_are_scrubbed(tracing):
    # The fixture initialized instrumentation already. Late credentials must be
    # explicitly registered, not rediscovered from the environment per call (S5).
    register_secret("fixture-key-secret")
    pipe = tracing.fakes.TextGenerationPipeline()
    pipe.result = [{"generated_text": "fixture-key-secret https://test/?sig=signed-value data:audio/wav;base64,QUJDRA=="}]
    pipe("fixture-key-secret")
    values = repr(spans(tracing)[0].attributes)
    for secret in ("fixture-key-secret", "signed-value", "QUJDRA=="):
        assert secret not in values


def test_direct_generate_identity_counts_with_padding_and_eos(tracing):
    model = tracing.fakes.Model()
    model.result = [[0, 1, 2, 3, 99, 0], [1, 2, 3, 4, 4, 99]]
    ids = [[0, 1, 2], [1, 2, 3]]
    mask = [[0, 1, 1], [1, 1, 1]]
    assert model.generate(ids, attention_mask=mask) is model.result
    assert model.calls[0][0] is ids and model.calls[0][1]["attention_mask"] is mask
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 5
    assert attrs["gen_ai.usage.output_tokens"] == 5
    assert attrs["gen_ai.usage.total_tokens"] == 10
    assert "output.value" not in attrs


def test_encoder_decoder_counts_exclude_decoder_start(tracing):
    model = tracing.fakes.Model()
    model.config.is_encoder_decoder = True
    model.result = [[0, 3, 4, 99]]
    model.generate([[1, 2]])
    assert spans(tracing)[0].attributes["gen_ai.usage.output_tokens"] == 3


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text", "hide_input_messages"])
@pytest.mark.parametrize("decoder_ids,sequences", [
    ([[42, 43]], [[0, 42, 43, 3, 4]]),
    ([[0, 42, 43]], [[0, 42, 43, 3, 4]]),
], ids=["prepended-start", "existing-start"])
def test_encoder_decoder_hidden_prompt_not_exported_as_output(tracing, flag, decoder_ids, sequences):
    reconfigure(tracing, **{flag: True})
    model = tracing.fakes.Model()
    model.config.is_encoder_decoder = True
    model.result = sequences
    vocabulary = {0: "", 1: "hello", 2: "world", 3: "new", 4: "tokens",
                  42: "PRIVATE_DECODER_42", 43: "PRIVATE_DECODER_43"}
    tokenizer = SimpleNamespace(batch_decode=lambda rows, **kwargs: [
        " ".join(vocabulary[token] for token in row).strip() for row in rows])
    assert model.generate([[1, 2]], decoder_input_ids=decoder_ids, tokenizer=tokenizer) is sequences
    assert len(model.calls) == 1
    assert model.calls[0][1]["decoder_input_ids"] is decoder_ids
    assert sequences == [[0, 42, 43, 3, 4]]
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.output.messages.0.message.content"] == "new tokens"
    assert attrs["gen_ai.usage.input_tokens"] == 2
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.total_tokens"] == 4
    assert "PRIVATE_DECODER" not in repr(attrs)


@pytest.mark.parametrize("decoder_ids,sequences", [
    ([[42, 43], [44, 45]], [[0, 42, 43, 3, 4], [0, 42, 43, 4, 3],
                           [0, 44, 45, 3, 3], [0, 44, 45, 4, 4]]),
    ([[0, 42], [44, 45]], [[0, 42, 3, 4], [0, 42, 4, 3],
                          [44, 45, 3, 3], [44, 45, 4, 4]]),
], ids=["prepended-start", "mixed-existing-start"])
def test_encoder_decoder_batched_prompts_multiple_returns(tracing, decoder_ids, sequences):
    model = tracing.fakes.Model()
    model.config.is_encoder_decoder = True
    model.result = sequences
    assert model.generate([[1, 2], [1, 2]], decoder_input_ids=decoder_ids) is sequences
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 4
    assert attrs["gen_ai.usage.output_tokens"] == 8
    assert attrs["gen_ai.usage.total_tokens"] == 12


def test_encoder_decoder_input_ids_alias_excludes_prompt(tracing):
    reconfigure(tracing, hide_inputs=True)
    model = tracing.fakes.Model()
    model.config.is_encoder_decoder = True
    model.main_input_name = "pixel_values"
    model.result = [[0, 1, 2, 3, 4]]
    tokenizer = tracing.fakes.Pipeline().tokenizer
    decoder_ids = [[1, 2]]
    assert model.generate(input_ids=decoder_ids, tokenizer=tokenizer) is model.result
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.output.messages.0.message.content"] == "new tokens"
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert "hello" not in repr(attrs) and "world" not in repr(attrs)


@pytest.mark.parametrize("case", ["unknown-prefix", "oversized-prefix", "unknown-batch-layout"])
def test_encoder_decoder_unverifiable_prompt_omits_output(tracing, case):
    model = tracing.fakes.Model()
    model.config.is_encoder_decoder = True
    decoder_ids = [[42, 43]]
    model.result = [[0, 42, 3, 4]]
    if case == "oversized-prefix":
        decoder_ids = [[42] * (extract.MAX_TOKEN_IDS + 1)]
    elif case == "unknown-batch-layout":
        decoder_ids = [[42, 43], [44, 45]]
        model.result = [[0, 42, 43, 3], [0, 44, 45, 4], [0, 44, 45, 3]]
    tokenizer = tracing.fakes.Pipeline().tokenizer
    assert model.generate([[1, 2]], decoder_input_ids=decoder_ids, tokenizer=tokenizer) is model.result
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 2
    assert "gen_ai.usage.output_tokens" not in attrs
    assert "gen_ai.usage.total_tokens" not in attrs
    assert not any(key.startswith(("output.", "gen_ai.output.")) for key in attrs)


def test_direct_tokenizer_decoding(tracing):
    model = tracing.fakes.Model()
    tokenizer = tracing.fakes.Pipeline().tokenizer
    model.generate([[1, 2]], tokenizer=tokenizer)
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.input.messages.0.message.content"] == "hello world"
    assert attrs["gen_ai.output.messages.0.message.content"] == "new tokens"


def test_streamer_identity_original_methods_and_ttft(tracing):
    streamer = tracing.fakes.TextStreamer()
    model = tracing.fakes.Model()
    original_result = streamer.result
    assert model.generate([[1, 2]], streamer=streamer) is model.result
    assert model.calls[0][1]["streamer"] is streamer
    assert streamer.received == [[[1, 2]], [[3]], [[4]]]
    assert streamer.put([[5]]) is original_result
    attrs = spans(tracing)[0].attributes
    assert 0 <= attrs["gen_ai.response.time_to_first_token"] <= attrs["gen_ai.response.latency"]
    assert attrs["gen_ai.server.time_to_first_token"] == attrs["gen_ai.response.time_to_first_token"] * 1000


def test_streamer_prompt_alone_has_no_ttft(tracing, monkeypatch):
    model = tracing.fakes.Model()
    # Simulate no new tokens (a custom generation implementation uses the hook).
    wrapper = _wrappers.GenerateWrapper(tracing.instrumentor, TraceConfig())
    streamer = tracing.fakes.TextStreamer()
    def only_prompt(*args, **kwargs):
        kwargs["streamer"].put(args[0])
        return args[0]
    result = [[1, 2]]
    assert wrapper(only_prompt, model, (result,), {"streamer": streamer}) is result
    assert "gen_ai.response.time_to_first_token" not in spans(tracing)[0].attributes


@pytest.mark.parametrize("error", [ValueError("credential-like prompt"), KeyboardInterrupt(), GeneratorExit()])
def test_error_and_cancellation_identity_no_retry_no_error_content(tracing, error):
    model = tracing.fakes.Model()
    model.error = error
    with pytest.raises(type(error)) as caught:
        model.generate([[1, 2]])
    assert caught.value is error and len(model.calls) == 1
    span = spans(tracing)[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == type(error).__name__
    assert "credential-like prompt" not in repr(span.attributes)
    assert not span.events and span.status.description is None
    # Context is restored even for BaseException cancellation.
    assert _wrappers._ACTIVE.get() is None


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("error", [None, ValueError("application stopped"), KeyboardInterrupt()])
def test_broken_tracer_fails_open(tracing, monkeypatch, streaming, error):
    class BrokenTracer:
        def start_span(self, *args, **kwargs):
            raise RuntimeError("telemetry unavailable")
    monkeypatch.setattr(tracing.instrumentor, "_tracer", BrokenTracer())
    model = tracing.fakes.Model()
    streamer = tracing.fakes.TextStreamer() if streaming else None
    original_put = tracing.fakes.TextStreamer.put
    observed_states = []

    def put(instance, value):
        observed_states.append((_wrappers._ACTIVE.get(), _wrappers._STREAM.get(), _wrappers._GENERATING.get()))
        result = original_put(instance, value)
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(tracing.fakes.TextStreamer, "put", put)
    if not streaming:
        model.error = error
    if error is None:
        assert model.generate([[1, 2]], streamer=streamer) is model.result
    else:
        with pytest.raises(type(error)) as caught:
            model.generate([[1, 2]], streamer=streamer)
        assert caught.value is error
    assert len(model.calls) == 1 and not spans(tracing)
    assert all(state == (None, None, False) for state in observed_states)
    assert _wrappers._ACTIVE.get() is None
    assert _wrappers._STREAM.get() is None
    assert not _wrappers._GENERATING.get()


def test_extractor_failure_still_ends_span_and_preserves_result(tracing, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("extractor failure")
    monkeypatch.setattr(extract, "generation_attributes", broken)
    model = tracing.fakes.Model()
    assert model.generate([[1, 2]]) is model.result
    assert len(spans(tracing)) == 1
    assert spans(tracing)[0].status.status_code is StatusCode.OK


def test_extraction_latency_not_in_span_duration(tracing, monkeypatch):
    original = extract.generation_attributes
    observed = []
    def delayed(*args, **kwargs):
        observed.append(time.time_ns())
        return original(*args, **kwargs)
    monkeypatch.setattr(extract, "generation_attributes", delayed)
    tracing.fakes.Model().generate([[1, 2]])
    span = spans(tracing)[0]
    assert span.end_time <= observed[0]


def test_suppression_and_context_parent(tracing):
    tracer = tracing.provider.get_tracer("application")
    with tracer.start_as_current_span("parent") as parent:
        tracing.fakes.Model().generate([[1, 2]])
        with suppress_tracing():
            tracing.fakes.TextGenerationPipeline()("hello")
    model_span = next(span for span in spans(tracing) if span.name.startswith("transformers."))
    assert model_span.parent.span_id == parent.get_span_context().span_id
    assert len(spans(tracing)) == 2


def test_idempotence_uninstrument_and_reinstrument(tracing):
    model = tracing.fakes.Model()
    patched = vars(tracing.fakes.GenerationMixin)["generate"]
    tracing.instrumentor.instrument(tracer_provider=tracing.provider)
    assert vars(tracing.fakes.GenerationMixin)["generate"] is patched
    model.generate([[1, 2]])
    assert len(spans(tracing)) == 1
    tracing.instrumentor.uninstrument()
    assert vars(tracing.fakes.GenerationMixin)["generate"] is patched.__wrapped__
    model.generate([[1, 2]])
    assert len(spans(tracing)) == 1
    tracing.instrumentor.instrument(tracer_provider=tracing.provider)
    model.generate([[1, 2]])
    assert len(spans(tracing)) == 2


def test_uninstrument_does_not_clobber_later_wrapper(tracing):
    from wrapt import FunctionWrapper
    cls = tracing.fakes.GenerationMixin
    ours = vars(cls)["generate"]
    later = FunctionWrapper(ours, lambda wrapped, instance, args, kwargs: wrapped(*args, **kwargs))
    cls.generate = later
    tracing.instrumentor.uninstrument()
    assert vars(cls)["generate"] is later
    assert tracing.fakes.Model().generate([[1, 2]]) == [[1, 2, 3, 4]]
    assert not spans(tracing)


def test_lazy_inputs_not_consumed_or_wrapped(tracing):
    pipe = tracing.fakes.Pipeline()
    consumed = []
    def source():
        consumed.append(True)
        yield "hello"
    iterator = source()
    pipe.result = iterator
    assert pipe(iterator) is iterator
    assert not consumed
    # The fake eagerly calls generate: it is the only span, not a pipeline span.
    assert len(spans(tracing)) == 1
    assert "input.value" not in spans(tracing)[0].attributes


def test_unknown_objects_not_stringified(tracing):
    class Private:
        def __str__(self):
            raise AssertionError("must not stringify")
        def __repr__(self):
            raise AssertionError("must not repr")
    model = tracing.fakes.Model()
    model.generate([[1, 2]], arbitrary=Private())
    assert "arbitrary" not in repr(spans(tracing)[0].attributes)


def test_cyclic_and_oversized_content_is_bounded():
    cyclic = []
    cyclic.append(cyclic)
    assert extract.messages(cyclic) == []
    assert extract.matrix([[1] * (extract.MAX_TOKEN_IDS + 1)]) is None
    attrs = extract.message_attributes(extract.messages("x" * (extract.MAX_TEXT * 2)), "input")
    assert len(attrs["gen_ai.input.messages.0.message.content"]) == extract.MAX_TEXT


def test_concurrent_calls_keep_streamers_and_spans_isolated(tracing):
    def generate(_):
        model = tracing.fakes.Model()
        streamer = tracing.fakes.TextStreamer()
        assert model.generate([[1, 2]], streamer=streamer) is model.result
        return streamer
    with ThreadPoolExecutor(max_workers=2) as pool:
        streamers = list(pool.map(generate, range(6)))
    assert len(spans(tracing)) == 6
    assert all(len(streamer.received) == 3 for streamer in streamers)
    assert all("gen_ai.response.time_to_first_token" in span.attributes for span in spans(tracing))


def test_native_intermediate_span_does_not_duplicate_generate(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    original = pipe.model.generate
    native = tracing.provider.get_tracer("sdk.native")
    def generate(*args, **kwargs):
        with native.start_as_current_span("native"):
            return original(*args, **kwargs)
    pipe.model.generate = generate
    assert pipe("hello world") is pipe.result
    assert len(spans(tracing)) == 2
    assert sum(span.attributes.get("gen_ai.span.kind") == "LLM" for span in spans(tracing)) == 1


def test_generation_config_overrides_eos_and_parameters(tracing):
    model = tracing.fakes.Model()
    model.result = [[1, 2, 3, 42, 0, 0]]
    config = SimpleNamespace(eos_token_id=42, max_new_tokens=8, temperature=0.75)
    model.generate([[1, 2]], config)
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.request.max_tokens"] == 8
    assert attrs["gen_ai.request.temperature"] == 0.75


def test_broken_tokenizer_preserves_id_usage(tracing):
    class BrokenTokenizer:
        def batch_decode(self, *args, **kwargs):
            raise ValueError("unavailable")
    tracing.fakes.Model().generate([[1, 2]], tokenizer=BrokenTokenizer())
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.usage.total_tokens"] == 4
    assert "output.value" not in attrs


def test_total_finalizer_failure_still_ends_span(tracing, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("finalizer unavailable")
    monkeypatch.setattr(_wrappers._Call, "finish", broken)
    model = tracing.fakes.Model()
    assert model.generate([[1, 2]]) is model.result
    assert len(spans(tracing)) == 1
    assert _wrappers._ACTIVE.get() is None


def test_reinstrument_retained_third_party_wrapper_is_not_reactivated(tracing):
    from wrapt import FunctionWrapper
    cls = tracing.fakes.GenerationMixin
    cls.generate = FunctionWrapper(vars(cls)["generate"],
                                   lambda wrapped, instance, args, kwargs: wrapped(*args, **kwargs))
    tracing.instrumentor.uninstrument()
    tracing.instrumentor.instrument(tracer_provider=tracing.provider)
    tracing.fakes.Model().generate([[1, 2]], streamer=tracing.fakes.TextStreamer())
    assert len(spans(tracing)) == 1
    assert spans(tracing)[0].attributes["gen_ai.usage.output_tokens"] == 2


def test_large_message_aggregates_are_bounded_valid_json():
    items = [{"role": "user", "content": "\\x00" * extract.MAX_TEXT} for _ in range(128)]
    attrs = extract.message_attributes(items, "input")
    assert len(attrs["input.value"]) <= extract.MAX_TEXT
    assert type(json.loads(attrs["input.value"])) is list
    assert len(attrs["gen_ai.input.messages.0.message.content"]) == extract.MAX_TEXT


def test_batched_chat_and_generations_are_bounded(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    prompts = [[{"role": "user", "content": "one"}], [{"role": "user", "content": "two"}]]
    pipe.result = [[{"generated_text": prompts[0] + [{"role": "assistant", "content": "reply one"}]}],
                   [{"generated_text": prompts[1] + [{"role": "assistant", "content": "reply two"}]}]]
    assert pipe(prompts) is pipe.result
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.input.messages.1.message.content"] == "two"
    assert attrs["gen_ai.output.messages.1.message.content"] == "reply two"


def test_streamer_exception_identity_and_error_span(tracing):
    error = ValueError("streamer stopped")
    class BrokenStreamer(tracing.fakes.TextStreamer):
        def put(self, value):
            super().put(value)
            raise error
    model = tracing.fakes.Model()
    with pytest.raises(ValueError) as caught:
        model.generate([[1, 2]], streamer=BrokenStreamer())
    assert caught.value is error
    assert len(model.calls) == 1
    assert spans(tracing)[0].status.status_code is StatusCode.ERROR


def test_pipeline_internal_failure_preserves_exception(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    error = RuntimeError("model failed")
    pipe.model.error = error
    with pytest.raises(RuntimeError) as caught:
        pipe("hello")
    assert caught.value is error
    assert len(spans(tracing)) == 1
    assert spans(tracing)[0].status.status_code is StatusCode.ERROR


def test_chat_mapping_content_parts_and_string_normalization():
    class NoStringHooks(str):
        def __str__(self):
            raise AssertionError("must not stringify")

        def __getitem__(self, key):
            raise AssertionError("must use builtin string slicing")

    source = [[UserDict(role=NoStringHooks("user"), content=[
        OrderedDict(type="text", text=NoStringHooks("PRIVATE_PREFIX")),
        UserDict(type="image", url="private-image"),
    ])]]
    result = [[OrderedDict(generated_text=[UserDict(role="user", content="PRIVATE_PREFIX reply")])]]
    assert extract.output_messages(result, source, {}) == [{"role": "user", "content": " reply"}]
    assert extract.messages(source) == [{"role": "user", "content": "PRIVATE_PREFIX"}]


@pytest.mark.parametrize("configuration,expected", [
    ("pipeline", 3), ("forward-config", 4), ("forward-kwarg", 5),
    ("call-config", 6), ("forward-kwarg-over-call-config", 5), ("call-kwarg", 7),
])
def test_pipeline_effective_generation_configuration(tracing, configuration, expected):
    pipe = tracing.fakes.TextGenerationPipeline()
    pipe.generation_config = SimpleNamespace(max_new_tokens=3)
    kwargs = {}
    if configuration == "forward-config":
        pipe._forward_params = {"generation_config": SimpleNamespace(max_new_tokens=4)}
    if configuration in ("forward-kwarg", "forward-kwarg-over-call-config", "call-kwarg"):
        pipe._forward_params = {"max_new_tokens": 5}
    if configuration in ("call-config", "forward-kwarg-over-call-config", "call-kwarg"):
        kwargs["generation_config"] = SimpleNamespace(max_new_tokens=6)
    if configuration == "call-kwarg":
        kwargs["max_new_tokens"] = 7
    pipe("hello", **kwargs)
    assert spans(tracing)[0].attributes["gen_ai.request.max_tokens"] == expected


def test_embedding_only_generation_uses_returned_ids_for_output_usage(tracing):
    model = tracing.fakes.Model()
    model.result = [[3, 4, 99]]
    mask = [[1, 1, 1, 1, 1, 1, 1]]
    assert model.generate(inputs_embeds=object(), attention_mask=mask) is model.result
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 7
    assert attrs["gen_ai.usage.output_tokens"] == 3
    assert attrs["gen_ai.usage.total_tokens"] == 10


def test_embedding_prompt_with_unverifiable_ids_omits_output(tracing):
    reconfigure(tracing, hide_inputs=True)
    model = tracing.fakes.Model()
    tokenizer = tracing.fakes.Pipeline().tokenizer
    model.generate(input_ids=[[1] * (extract.MAX_TOKEN_IDS + 1)], inputs_embeds=object(),
                   attention_mask=[[1, 1]], tokenizer=tokenizer)
    attrs = spans(tracing)[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 2
    assert "gen_ai.usage.output_tokens" not in attrs
    assert not any(key.startswith(("output.", "gen_ai.output.")) for key in attrs)


def test_generation_defers_only_sequence_ids_not_returned_caches(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    sequences = [[1, 2, 3, 4]]
    pipe.model.result = {"sequences": sequences, "past_key_values": object()}
    generate = pipe.model.generate

    def observed(*args, **kwargs):
        result = generate(*args, **kwargs)
        assert result is pipe.model.result
        assert _wrappers._ACTIVE.get().generations[-1][-1] is sequences
        return result

    pipe.model.generate = observed
    pipe("hello world")
    assert spans(tracing)[0].attributes["gen_ai.usage.output_tokens"] == 2


def test_batch_content_cannot_evict_essential_metadata(tracing):
    pipe = tracing.fakes.TextGenerationPipeline()
    pipe.result = [[{"generated_text": "hello reply"}] for _ in range(32)]
    pipe(["hello"] * 32)
    attrs = spans(tracing)[0].attributes
    for key in ("gen_ai.span.kind", "gen_ai.operation.name", "gen_ai.provider.name",
                "traceai.capture.layer", "gen_ai.request.model", "gen_ai.response.model",
                "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.response.latency"):
        assert key in attrs
