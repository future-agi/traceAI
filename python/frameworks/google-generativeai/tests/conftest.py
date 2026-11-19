"""Offline harness: the real google.generativeai SDK with a fake transport client. No network, no keys."""
import socket

import pytest
from google.generativeai import protos
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

MODEL = "models/gemini-1.5-flash"
FinishReason = protos.Candidate.FinishReason


def text_response(text, *, finish=FinishReason.STOP, usage=(5, 7), model_version="gemini-1.5-flash-002"):
    kwargs = {}
    if usage:
        kwargs["usage_metadata"] = protos.GenerateContentResponse.UsageMetadata(
            prompt_token_count=usage[0], candidates_token_count=usage[1], total_token_count=usage[0] + usage[1]
        )
    return protos.GenerateContentResponse(
        candidates=[protos.Candidate(
            content=protos.Content(role="model", parts=[protos.Part(text=text)]),
            finish_reason=finish,
        )],
        model_version=model_version,
        **kwargs,
    )


def call_response(name, args):
    return protos.GenerateContentResponse(
        candidates=[protos.Candidate(
            content=protos.Content(role="model", parts=[protos.Part(function_call=protos.FunctionCall(name=name, args=args))]),
            finish_reason=FinishReason.STOP,
        )],
        usage_metadata=protos.GenerateContentResponse.UsageMetadata(
            prompt_token_count=3, candidates_token_count=2, total_token_count=5),
    )


class FakeClient:
    def __init__(self):
        self.requests = []
        self.responses = []
        self.chunks = []
        self.error = None
        self.embeddings = None

    def generate_content(self, request, **kwargs):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.responses.pop(0)

    def stream_generate_content(self, request, **kwargs):
        self.requests.append(request)
        if self.error:
            raise self.error
        return iter(self.chunks)

    def embed_content(self, request, **kwargs):
        self.requests.append(request)
        return protos.EmbedContentResponse(embedding=protos.ContentEmbedding(values=[0.5, 0.25, 0.125]))

    def batch_embed_contents(self, request, **kwargs):
        self.requests.append(request)
        return protos.BatchEmbedContentsResponse(
            embeddings=[protos.ContentEmbedding(values=[0.5, 0.25]) for _ in request.requests])


class FakeAsyncClient(FakeClient):
    async def generate_content(self, request, **kwargs):
        return FakeClient.generate_content(self, request, **kwargs)

    async def stream_generate_content(self, request, **kwargs):
        self.requests.append(request)
        if self.error:
            raise self.error
        chunks = list(self.chunks)

        async def gen():
            for chunk in chunks:
                yield chunk

        return gen()

    async def embed_content(self, request, **kwargs):
        return FakeClient.embed_content(self, request, **kwargs)

    async def batch_embed_contents(self, request, **kwargs):
        return FakeClient.batch_embed_contents(self, request, **kwargs)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def make_spans():
    started = []

    def start(**kwargs):
        from traceai_google_generativeai import GoogleGenerativeAIInstrumentor

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        instrumentor = GoogleGenerativeAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        started.append((instrumentor, provider))
        return exporter

    yield start
    for instrumentor, provider in reversed(started):
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.fixture
def spans(make_spans):
    return make_spans()


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def aclient():
    return FakeAsyncClient()


@pytest.fixture
def model(client):
    import google.generativeai as genai

    instance = genai.GenerativeModel(MODEL)
    instance._client = client
    return instance


@pytest.fixture
def amodel(aclient):
    import google.generativeai as genai

    instance = genai.GenerativeModel(MODEL)
    instance._async_client = aclient
    return instance


def one(exporter):
    finished = exporter.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]
    return finished[0], dict(finished[0].attributes)
