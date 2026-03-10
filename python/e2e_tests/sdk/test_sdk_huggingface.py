"""
E2E Tests for HuggingFace SDK Instrumentation

Tests HuggingFace InferenceClient with tracing. Requires HF_API_KEY.
"""

import pytest
import time

from config import config, skip_if_no_hf


@pytest.fixture(scope="module")
def hf_client():
    """Create an instrumented HuggingFace client."""
    if not config.has_hf():
        pytest.skip("HF_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_huggingface import HuggingFaceInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_huggingface not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    HuggingFaceInstrumentor().instrument(tracer_provider=tracer_provider)

    from huggingface_hub import InferenceClient

    client = InferenceClient(token=config.hf_api_key)

    yield client

    HuggingFaceInstrumentor().uninstrument()


@skip_if_no_hf
class TestHuggingFaceChatCompletion:
    """Test HuggingFace chat completion instrumentation."""

    def test_basic_chat(self, hf_client):
        """Test basic chat completion."""
        response = hf_client.chat_completion(
            model="meta-llama/Llama-3.2-3B-Instruct",
            messages=[
                {"role": "user", "content": "Say 'Hello E2E Test' in exactly 3 words."}
            ],
            max_tokens=50,
        )

        assert response.choices[0].message.content is not None
        time.sleep(2)
        print(f"Response: {response.choices[0].message.content}")

    def test_streaming(self, hf_client):
        """Test streaming chat completion."""
        chunks = []

        stream = hf_client.chat_completion(
            model="meta-llama/Llama-3.2-3B-Instruct",
            messages=[
                {"role": "user", "content": "Count from 1 to 5."}
            ],
            max_tokens=50,
            stream=True,
        )

        for chunk in stream:
            if chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_system_message(self, hf_client):
        """Test with system + user messages."""
        response = hf_client.chat_completion(
            model="meta-llama/Llama-3.2-3B-Instruct",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that responds briefly."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None


@skip_if_no_hf
class TestHuggingFaceEmbeddings:
    """Test HuggingFace embeddings."""

    def test_feature_extraction(self, hf_client):
        """Test feature extraction / embeddings."""
        response = hf_client.feature_extraction(
            "Hello, world!",
            model="sentence-transformers/all-MiniLM-L6-v2",
        )

        assert response is not None
        assert len(response) > 0
        print(f"Embedding shape: {len(response)}")


@skip_if_no_hf
class TestHuggingFaceErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, hf_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            hf_client.chat_completion(
                model="invalid-model-xyz/does-not-exist",
                messages=[{"role": "user", "content": "test"}],
            )
