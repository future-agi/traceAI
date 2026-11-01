"""Real, tiny CPU models constructed locally: no Hub access or credentials."""
import concurrent.futures
import copy
import gc
import gzip
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import pytest
import torch
import transformers
from fi_instrumentation import TraceConfig
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from traceai_transformers import TransformersInstrumentor
from transformers import (
    BertConfig,
    BertModel,
    GPT2Config,
    GPT2LMHeadModel,
    PreTrainedTokenizerFast,
    TextIteratorStreamer,
    pipeline,
)

torch.set_num_threads(1)
HISTORICAL = int(transformers.__version__.split(".")[0]) < 5


@pytest.fixture(scope="module")
def tokenizer():
    vocab = {word: i for i, word in enumerate(
        ["[PAD]", "[UNK]", "[BOS]", "[EOS]", "hello", "world", "a", "small", "model",
         "user", "assistant", "what", "is", "the", "answer", "context", "test", ":", "?"])}
    backend = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
    backend.pre_tokenizer = Whitespace()
    tok = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="[UNK]", pad_token="[PAD]",
                                 bos_token="[BOS]", eos_token="[EOS]", model_max_length=64)
    tok.chat_template = "{% for message in messages %}{{message['role'] + ': ' + message['content'] + ' '}}{% endfor %}{% if add_generation_prompt %}assistant: {% endif %}"
    return tok


@pytest.fixture(scope="module")
def generation(tokenizer):
    torch.manual_seed(42)
    model = GPT2LMHeadModel(GPT2Config(vocab_size=len(tokenizer), n_positions=64, n_layer=1,
                                     n_head=2, n_embd=8, bos_token_id=2, eos_token_id=3, pad_token_id=0))
    model.eval()  # Config-constructed models start in training mode (dropout).
    return pipeline("text-generation", model=model, tokenizer=tokenizer, device=-1)


@pytest.fixture(scope="module")
def features(tokenizer):
    torch.manual_seed(42)
    model = BertModel(BertConfig(vocab_size=len(tokenizer), hidden_size=8, num_hidden_layers=1,
                                num_attention_heads=2, intermediate_size=16))
    model.eval()
    return pipeline("feature-extraction", model=model, tokenizer=tokenizer, device=-1)


@pytest.fixture
def telemetry():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = TransformersInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    yield instrumentor, provider, exporter
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


def instrument(telemetry, **kwargs):
    instr, provider, exporter = telemetry
    instr.instrument(tracer_provider=provider, **kwargs)
    return exporter


def spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name.startswith("transformers.")]


def assert_same(a, b):
    if isinstance(a, torch.Tensor):
        assert torch.equal(a, b)
    elif isinstance(a, dict):
        assert a.keys() == b.keys()
        for key in a:
            assert_same(a[key], b[key])
    elif isinstance(a, list):
        assert len(a) == len(b)
        for x, y in zip(a, b):
            assert_same(x, y)
    else:
        assert a == b


@pytest.mark.parametrize("prompt,kwargs", [
    ("hello world", {}),
    (["hello world", "a small model"], {"batch_size": 2}),
    ([{"role": "user", "content": "hello world"}], {}),
    ([[{"role": "user", "content": "hello"}], [{"role": "user", "content": "world"}]], {}),
    ("hello", {"num_beams": 2, "num_return_sequences": 2}),
    ("hello", {"return_tensors": True}),
])
def test_generation_parity(generation, telemetry, prompt, kwargs):
    opts = dict(max_new_tokens=2, do_sample=False, **kwargs)
    original = copy.deepcopy(prompt)
    baseline = generation(prompt, **opts)
    exporter = instrument(telemetry, capture_content=True)
    result = generation(prompt, **opts)
    assert_same(baseline, result)
    assert prompt == original
    [span] = spans(exporter)
    assert span.attributes["gen_ai.span.kind"] == "LLM"
    assert span.attributes["gen_ai.request.model"] == "gpt2"
    assert span.status.status_code.name == "OK"
    assert len(span.attributes["input.value"]) <= 4096


@pytest.mark.parametrize("tensor", [False, True])
def test_features_parity(features, telemetry, tensor):
    baseline = features("hello world", return_tensors=tensor)
    exporter = instrument(telemetry, capture_content=True)
    result = features("hello world", return_tensors=tensor)
    assert_same(baseline, result)
    [span] = spans(exporter)
    assert span.attributes["gen_ai.span.kind"] == "EMBEDDING"
    assert span.attributes["gen_ai.operation.name"] == "embeddings"
    assert "output.value" not in span.attributes
    assert not any("vector" in k for k in span.attributes)


def test_default_metadata_only(generation, telemetry):
    exporter = instrument(telemetry)
    generation("SENTINEL-PRIVATE", max_new_tokens=1)
    [span] = spans(exporter)
    assert "SENTINEL-PRIVATE" not in str(span.attributes)
    assert "input.value" not in span.attributes
    assert "output.value" not in span.attributes


def test_idempotence_suppression_and_reinstrument(generation, telemetry):
    instr, provider, exporter = telemetry
    original = type(generation).__call__
    instrument(telemetry)
    instr.instrument(tracer_provider=provider)
    generation("hello", max_new_tokens=1)
    assert len(spans(exporter)) == 1
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        generation("hello", max_new_tokens=1)
    finally:
        context.detach(token)
    assert len(spans(exporter)) == 1
    instr.uninstrument()
    assert type(generation).__call__ is original
    generation("hello", max_new_tokens=1)
    assert len(spans(exporter)) == 1
    instr.instrument(tracer_provider=provider)
    generation("hello", max_new_tokens=1)
    assert len(spans(exporter)) == 2


def test_argument_error_is_unchanged(generation, telemetry):
    with pytest.raises(Exception) as baseline:
        generation("hello", definitely_not_a_generation_option="SENTINEL-ERROR")
    exporter = instrument(telemetry)
    with pytest.raises(type(baseline.value)) as actual:
        generation("hello", definitely_not_a_generation_option="SENTINEL-ERROR")
    assert str(actual.value) == str(baseline.value)
    [span] = spans(exporter)
    assert span.status.status_code.name == "ERROR"
    assert len(span.events) == 1


@pytest.mark.parametrize("mode", ["exhaust", "close", "error", "gc", "unused"])
@pytest.mark.parametrize("source_type", ["generator", "dataset"])
def test_lazy_lifetime(features, telemetry, mode, source_type):
    consumed = []
    def source():
        for i in range(3):
            consumed.append(i)
            if mode == "error" and i == 1:
                raise ValueError("SENTINEL-LAZY")
            yield "hello world"
    class Data(torch.utils.data.Dataset):
        def __len__(self):
            return 3
        def __getitem__(self, index):
            if index >= 3:
                raise IndexError
            consumed.append(index)
            if mode == "error" and index == 1:
                raise ValueError("SENTINEL-LAZY")
            return "hello world"
    def make_source():
        return source() if source_type == "generator" else Data()
    # Transformers 5 may peek once during setup. Instrumentation must not add
    # any consumption beyond upstream behavior.
    baseline = features(make_source(), num_workers=0)
    baseline_setup = list(consumed)
    del baseline
    consumed.clear()
    instr, provider, exporter = telemetry
    instrument(telemetry, capture_content=True)
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("parent") as parent:
        iterator = features(make_source(), num_workers=0)
        assert consumed == baseline_setup
        assert spans(exporter) == []
    if mode == "unused":
        iterator.close()
        assert spans(exporter) == []
        return
    first = next(iterator)
    assert first
    assert spans(exporter) == []
    assert not trace.get_current_span().get_span_context().is_valid
    if mode == "exhaust":
        list(iterator)
    elif mode == "close":
        iterator.close()
        iterator.close()
        with pytest.raises(StopIteration):
            next(iterator)
    elif mode == "error":
        with pytest.raises(ValueError, match="SENTINEL-LAZY"):
            next(iterator)
    else:
        del iterator
        gc.collect()
    [span] = spans(exporter)
    assert span.parent.span_id == parent.get_span_context().span_id
    assert span.status.status_code.name == ("ERROR" if mode == "error" else "OK")
    assert "input.value" not in span.attributes
    assert "output.value" not in span.attributes


def test_lazy_forward_parent_and_no_payload_accumulation(features, telemetry, monkeypatch):
    exporter = instrument(telemetry, capture_content=True)
    seen = []
    original = features._forward
    def forward(*args, **kwargs):
        seen.append(trace.get_current_span().get_span_context().span_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(features, "_forward", forward)
    iterator = features(("hello" for _ in range(100)), num_workers=0)
    count = 0
    for _ in iterator:
        count += 1
        assert not trace.get_current_span().get_span_context().is_valid
    [span] = spans(exporter)
    assert count == 100
    assert set(seen) == {span.context.span_id}
    assert "output.value" not in span.attributes
    assert not any(isinstance(v, list) for v in vars(iterator).values())


def test_streamer_thread_context_and_callback(generation, tokenizer, telemetry):
    instr, provider, exporter = telemetry
    baseline_stream = TextIteratorStreamer(tokenizer, timeout=10)
    baseline = generation("hello", max_new_tokens=3, do_sample=False, streamer=baseline_stream)
    baseline_tokens = list(baseline_stream)
    instrument(telemetry)
    callback_spans = []
    class Stream(TextIteratorStreamer):
        def put(self, value):
            callback_spans.append(trace.get_current_span().get_span_context().span_id)
            return super().put(value)
    streamer = Stream(tokenizer, timeout=10)
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("parent") as parent:
        parent_context = context.get_current()
        def work():
            token = context.attach(parent_context)
            try:
                return generation("hello", max_new_tokens=3, do_sample=False, streamer=streamer)
            finally:
                context.detach(token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(work).result(timeout=20)
        # Span ends when inference completes, before any queued token is read.
        [span] = spans(exporter)
        assert span.end_time <= time.time_ns()
        assert span.parent.span_id == parent.get_span_context().span_id
        assert set(callback_spans) == {span.context.span_id}
        assert_same(baseline, result)
        assert list(streamer) == baseline_tokens


def test_concurrent_isolation(features, telemetry):
    instr, provider, exporter = telemetry
    instrument(telemetry)
    barrier = threading.Barrier(2)
    def worker(index):
        with provider.get_tracer("test").start_as_current_span("parent-" + str(index)) as parent:
            barrier.wait(timeout=10)
            features("hello")
            return parent.get_span_context().span_id
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        parents = set(pool.map(worker, range(2)))
    assert len(spans(exporter)) == 2
    assert {s.parent.span_id for s in spans(exporter)} == parents
    assert len({s.context.trace_id for s in spans(exporter)}) == 2


@pytest.mark.skipif(not HISTORICAL, reason="Historical tasks removed by Transformers 5")
@pytest.mark.parametrize("task", ["text2text-generation", "summarization", "translation", "question-answering"])
def test_historical_pipeline_parity(tokenizer, telemetry, task):
    from transformers import (
        BertForQuestionAnswering,
        T5Config,
        T5ForConditionalGeneration,
    )
    if task == "question-answering":
        model = BertForQuestionAnswering(BertConfig(vocab_size=len(tokenizer), hidden_size=8,
            num_hidden_layers=1, num_attention_heads=2, intermediate_size=16))
        args, kwargs = (), {"question": "what is the answer ?", "context": "hello world is the answer"}
    else:
        model = T5ForConditionalGeneration(T5Config(vocab_size=len(tokenizer), d_model=8, d_ff=16,
            num_layers=1, num_decoder_layers=1, num_heads=2, decoder_start_token_id=0,
            pad_token_id=0, eos_token_id=3))
        args, kwargs = ("hello world",), {"max_new_tokens": 2, "do_sample": False}
    model.eval()
    pipe = pipeline(task, model=model, tokenizer=tokenizer, device=-1)
    baseline = pipe(*args, **kwargs)
    exporter = instrument(telemetry)
    actual = pipe(*args, **kwargs)
    assert_same(baseline, actual)
    [span] = spans(exporter)
    assert span.name == "transformers." + task
    assert span.attributes["gen_ai.span.kind"] == ("CHAIN" if task == "question-answering" else "LLM")


def test_hub_coexistence(generation, telemetry, monkeypatch):
    from huggingface_hub import InferenceClient
    from traceai_huggingface import HuggingFaceInstrumentor
    instr, provider, exporter = telemetry
    # Replace only remote I/O with a synthetic response; local inference is real.
    monkeypatch.setattr(InferenceClient, "text_generation", lambda self, *a, **k: "synthetic result")
    hub = HuggingFaceInstrumentor()
    original = InferenceClient.text_generation
    hub.instrument(tracer_provider=provider)
    remote_patch = vars(InferenceClient)["text_generation"]
    try:
        instrument(telemetry)
        assert vars(InferenceClient)["text_generation"] is remote_patch
        generation("hello", max_new_tokens=1)
        assert InferenceClient(model="synthetic").text_generation("hello") == "synthetic result"
        assert len(spans(exporter)) == 1
        assert len(exporter.get_finished_spans()) == 2
        instr.uninstrument()
        assert vars(InferenceClient)["text_generation"] is remote_patch
    finally:
        hub.uninstrument()
    assert InferenceClient.text_generation is original


@pytest.fixture
def receiver():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            request = ExportTraceServiceRequest.FromString(body)
            received.append((self.path, dict(self.headers), request))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/tracer/v1/traces", received
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("privacy", ["default", "config", "env", "text_flags", "inputs_only"])
def test_otlp_wire_and_privacy(generation, telemetry, receiver, monkeypatch, privacy):
    endpoint, received = receiver
    instr, provider, _ = telemetry
    exporter = OTLPSpanExporter(endpoint=endpoint, headers={"FI_API_KEY": "synthetic-api", "FI_SECRET_KEY": "synthetic-secret"})
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = None
    if privacy == "config":
        config = TraceConfig(hide_inputs=True, hide_outputs=True, hide_error_details=True)
    elif privacy == "env":
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
        monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
        monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    elif privacy == "text_flags":
        config = TraceConfig(hide_input_text=True, hide_output_text=True, hide_error_details=True)
    elif privacy == "inputs_only":
        config = TraceConfig(hide_inputs=True, hide_error_details=True)
    instrument(telemetry, config=config, capture_content=privacy != "default")
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        generation("SENTINEL-CONTENT", max_new_tokens=1)
        if privacy != "default":
            def fail(*args, **kwargs):
                raise ValueError("SENTINEL-ERROR")
            monkeypatch.setattr(generation, "_forward", fail)
            with pytest.raises(ValueError, match="SENTINEL-ERROR"):
                generation("SENTINEL-CONTENT", max_new_tokens=1)
    provider.force_flush()
    decoded = []
    for path, headers, request in received:
        assert path == "/tracer/v1/traces"
        lower = {k.lower(): v for k, v in headers.items()}
        assert lower["fi_api_key"] == "synthetic-api"
        assert lower["fi_secret_key"] == "synthetic-secret"
        assert "SENTINEL" not in str(request)
        for rs in request.resource_spans:
            for scope in rs.scope_spans:
                decoded.extend(s for s in scope.spans if s.name.startswith("transformers."))
    assert len(decoded) == (1 if privacy == "default" else 2)
    assert all(int.from_bytes(s.parent_span_id, "big") == parent.get_span_context().span_id for s in decoded)
    assert all(s.end_time_unix_nano >= s.start_time_unix_nano for s in decoded)
    if privacy != "default":
        assert decoded[-1].status.code == 2
        assert not decoded[-1].status.message
        assert all("exception.message" not in str(e) and "exception.stacktrace" not in str(e) for e in decoded[-1].events)


def test_no_model_construction_or_bare_generate_spans(tokenizer, telemetry):
    exporter = instrument(telemetry)
    model = GPT2LMHeadModel(GPT2Config(vocab_size=len(tokenizer), n_positions=64, n_layer=1,
        n_head=2, n_embd=8, bos_token_id=2, eos_token_id=3, pad_token_id=0))
    model.generate(torch.tensor([[4]]), max_new_tokens=1)
    assert spans(exporter) == []


def test_lazy_generation_parity(generation, telemetry):
    baseline = list(generation((text for text in ["hello", "world"]), max_new_tokens=2, do_sample=False))
    exporter = instrument(telemetry)
    iterator = generation((text for text in ["hello", "world"]), max_new_tokens=2, do_sample=False)
    assert spans(exporter) == []
    assert_same(baseline, list(iterator))
    [span] = spans(exporter)
    assert span.attributes["gen_ai.span.kind"] == "LLM"


def test_batch_feature_parity(features, telemetry):
    baseline = features(["hello world", "a small model"], batch_size=2)
    exporter = instrument(telemetry)
    assert_same(baseline, features(["hello world", "a small model"], batch_size=2))
    assert len(spans(exporter)) == 1


def test_streamer_exception_unchanged(generation, telemetry):
    from transformers.generation.streamers import BaseStreamer
    class FailingStreamer(BaseStreamer):
        def put(self, value):
            raise RuntimeError("SENTINEL-CALLBACK")
    exporter = instrument(telemetry, config=TraceConfig(hide_error_details=True))
    with pytest.raises(RuntimeError, match="SENTINEL-CALLBACK"):
        generation("hello", max_new_tokens=1, streamer=FailingStreamer())
    [span] = spans(exporter)
    assert span.status.status_code.name == "ERROR"
    assert "SENTINEL" not in str(span.attributes) + str(span.events) + str(span.status.description)


def test_uninstrument_keeps_outer_patch(generation, telemetry):
    from wrapt import FunctionWrapper
    instr, _, exporter = telemetry
    cls = type(generation)
    original = vars(cls)["__call__"]
    instrument(telemetry)
    ours = vars(cls)["__call__"]
    outer = FunctionWrapper(ours, lambda wrapped, instance, args, kwargs: wrapped(*args, **kwargs))
    cls.__call__ = outer
    try:
        instr.uninstrument()
        assert vars(cls)["__call__"] is outer
        generation("hello", max_new_tokens=1)
        assert spans(exporter) == []
        instrument(telemetry)
        generation("hello", max_new_tokens=1)
        assert len(spans(exporter)) == 1
        instr.uninstrument()
        assert vars(cls)["__call__"] is outer
    finally:
        cls.__call__ = original


def test_preview_is_bounded_without_user_serializers():
    from traceai_transformers._wrappers import _preview
    class Private:
        def __repr__(self):
            raise AssertionError("must not run user repr")
        def __iter__(self):
            raise AssertionError("must not consume arbitrary iterables")
    assert _preview(Private()) == '"[not captured]"'
    large = ["x" * 10000] * 100000
    assert len(_preview(large)) <= 4096


def test_telemetry_failure_does_not_change_inference(generation, telemetry, monkeypatch, caplog):
    from fi_instrumentation import FITracer
    baseline = generation("hello", max_new_tokens=1, do_sample=False)
    instrument(telemetry)
    def fail(*args, **kwargs):
        raise RuntimeError("broken provider")
    monkeypatch.setattr(FITracer, "start_span", fail)
    assert_same(baseline, generation("hello", max_new_tokens=1, do_sample=False))
    assert "failed to start span (RuntimeError)" in caplog.text
    assert "broken provider" not in caplog.text


@pytest.mark.parametrize("kwargs", [{"config": object()}, {"capture_content": "yes"}])
def test_invalid_configuration_does_not_patch(generation, telemetry, kwargs):
    original = vars(type(generation))["__call__"]
    with pytest.raises(TypeError):
        instrument(telemetry, **kwargs)
    assert vars(type(generation))["__call__"] is original
