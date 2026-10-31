"""Pytest fixtures for vLLM tests."""

from unittest.mock import MagicMock, PropertyMock
import pytest


@pytest.fixture
def mock_openai_response():
    """Create a mock OpenAI-compatible response."""
    response = MagicMock()
    response.id = "chatcmpl-123"
    response.model = "meta-llama/Llama-2-7b-chat-hf"
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock()
    response.choices[0].message.role = "assistant"
    response.choices[0].message.content = "Hello! How can I help you?"
    response.choices[0].finish_reason = "stop"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 8
    response.usage.total_tokens = 18
    response.model_dump = MagicMock(return_value={
        "id": "chatcmpl-123",
        "model": "meta-llama/Llama-2-7b-chat-hf",
        "choices": [{"message": {"role": "assistant", "content": "Hello! How can I help you?"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    })
    return response


@pytest.fixture
def mock_stream_chunks():
    """Create mock streaming chunks."""
    chunks = []
    for i, content in enumerate(["Hello", "!", " How", " can", " I", " help", "?"]):
        chunk = MagicMock()
        chunk.id = "chatcmpl-123"
        chunk.model = "meta-llama/Llama-2-7b-chat-hf"
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta = MagicMock()
        chunk.choices[0].delta.content = content
        chunk.choices[0].finish_reason = None if i < 6 else "stop"
        chunk.usage = None
        chunk.model_dump = MagicMock(return_value={"id": f"chunk-{i}", "content": content})
        chunks.append(chunk)

    # Last chunk has usage
    chunks[-1].usage = MagicMock()
    chunks[-1].usage.prompt_tokens = 10
    chunks[-1].usage.completion_tokens = 7
    chunks[-1].usage.total_tokens = 17
    return chunks


@pytest.fixture
def mock_vllm_client():
    """Create a mock client configured for vLLM."""
    client = MagicMock()
    client._client = MagicMock()
    type(client._client).base_url = PropertyMock(return_value="http://localhost:8000/v1")
    return client


@pytest.fixture
def mock_non_vllm_client():
    """Create a mock client NOT configured for vLLM."""
    client = MagicMock()
    client._client = MagicMock()
    type(client._client).base_url = PropertyMock(return_value="https://api.openai.com/v1")
    return client


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """These unit/serialization contracts must never contact a provider."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Network calls are forbidden in provider unit tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def export_attributes():
    """Exercise the real FITracer privacy boundary, not a mocked constants table."""
    from fi_instrumentation import FITracer, TraceConfig
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def export(attributes, **privacy):
        exporter.clear()
        tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(**privacy))
        with tracer.start_as_current_span("provider-contract") as span:
            span.set_attributes(attributes)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return dict(spans[0].attributes)

    yield export
    provider.shutdown()
