"""
E2E Tests for Ollama SDK Instrumentation

Tests Ollama native SDK with tracing. Requires a local Ollama server.
Set OLLAMA_HOST to enable these tests.
"""

import pytest
import time

from config import config, skip_if_no_ollama


@pytest.fixture(scope="module")
def ollama_client():
    """Create an instrumented Ollama client."""
    if not config.has_ollama():
        pytest.skip("OLLAMA_HOST not set")

    from fi_instrumentation import register
    try:
        from traceai_ollama import OllamaInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_ollama not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    OllamaInstrumentor().instrument(tracer_provider=tracer_provider)

    import ollama

    client = ollama.Client(host=config.ollama_host)

    yield client

    OllamaInstrumentor().uninstrument()


@skip_if_no_ollama
class TestOllamaChatCompletion:
    """Test Ollama chat completion instrumentation."""

    def test_basic_chat(self, ollama_client):
        """Test basic chat completion."""
        response = ollama_client.chat(
            model="llama3.2",
            messages=[
                {"role": "user", "content": "Say 'Hello E2E Test' in exactly 3 words."}
            ],
        )

        assert response["message"]["content"] is not None
        time.sleep(2)
        print(f"Response: {response['message']['content']}")

    def test_streaming(self, ollama_client):
        """Test streaming chat completion."""
        chunks = []

        stream = ollama_client.chat(
            model="llama3.2",
            messages=[
                {"role": "user", "content": "Count from 1 to 5."}
            ],
            stream=True,
        )

        for chunk in stream:
            if chunk["message"]["content"]:
                chunks.append(chunk["message"]["content"])

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_system_message(self, ollama_client):
        """Test with system + user messages."""
        response = ollama_client.chat(
            model="llama3.2",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that responds briefly."},
                {"role": "user", "content": "What is 2+2?"},
            ],
        )

        assert response["message"]["content"] is not None
        assert "4" in response["message"]["content"]

    def test_generate(self, ollama_client):
        """Test generate (non-chat) endpoint."""
        response = ollama_client.generate(
            model="llama3.2",
            prompt="What is 2+2? Answer in one word.",
        )

        assert response["response"] is not None
        print(f"Generate response: {response['response']}")


@skip_if_no_ollama
class TestOllamaEmbeddings:
    """Test Ollama embeddings."""

    def test_embeddings(self, ollama_client):
        """Test embedding generation."""
        response = ollama_client.embed(
            model="llama3.2",
            input="Hello, world!",
        )

        assert response["embeddings"] is not None
        assert len(response["embeddings"]) > 0
        print(f"Embedding dimensions: {len(response['embeddings'][0])}")


@skip_if_no_ollama
class TestOllamaErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, ollama_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            ollama_client.chat(
                model="nonexistent-model-xyz",
                messages=[{"role": "user", "content": "test"}],
            )
