"""Offline SageMaker Runtime harness: a real boto3 client, a canned response, no network.

Responses are injected through botocore's documented ``before-call`` event, which short-circuits
the HTTP request after parameter validation. The client is real, so the instrumentor is exercised
through ``ClientCreator.create_client`` exactly as an application (or zero-code auto) reaches it.
"""
import io
import json
from types import SimpleNamespace

import boto3
import pytest
from botocore.eventstream import EventStream
from botocore.response import StreamingBody
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

ENDPOINT = "llm-endpoint-1"


class FakeEventStream(EventStream):
    """A botocore EventStream without an HTTP response, parser or network."""

    def __init__(self, events, error=None):
        self.events = events
        self.error = error
        self.consumed = 0
        self.close_calls = 0

    def __iter__(self):
        for event in self.events:
            self.consumed += 1
            yield event
        if self.error is not None:
            raise self.error

    def close(self):
        self.close_calls += 1


def part(data):
    if isinstance(data, str):
        data = data.encode()
    return {"PayloadPart": {"Bytes": data}}


def body_of(data):
    if not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode()
    return StreamingBody(io.BytesIO(bytes(data)), len(data))


def make_client(service="sagemaker-runtime"):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def respond(client, operation, parsed=None, status=200, service="sagemaker-runtime"):
    """Answer ``operation`` with a canned parsed response and return the recorded requests."""
    seen = []

    def handler(params=None, **_):
        seen.append(params)
        return SimpleNamespace(status_code=status, headers={}), dict(parsed or {})

    client.meta.events.register(f"before-call.{service}.{operation}", handler)
    return seen


def ok(body, content_type="application/json", **extra):
    parsed = {
        "Body": body_of(body) if not isinstance(body, (StreamingBody, FakeEventStream)) else body,
        "ContentType": content_type,
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }
    parsed.update(extra)
    return parsed


def attrs(span):
    return dict(span.attributes)


def blob(span):
    return json.dumps(attrs(span), default=str)


@pytest.fixture
def provider():
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer_provider.exporter = exporter
    yield tracer_provider
    tracer_provider.shutdown()


@pytest.fixture
def instrument(provider):
    from traceai_sagemaker import SageMakerInstrumentor

    instrumentors = []

    def start(**kwargs):
        instrumentor = SageMakerInstrumentor()
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        instrumentors.append(instrumentor)
        return provider.exporter

    yield start
    for instrumentor in reversed(instrumentors):
        instrumentor.uninstrument()


@pytest.fixture
def spans(instrument):
    return instrument()
