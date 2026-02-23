"""
E2E Tests for vLLM SDK Instrumentation

Tests vLLM instrumentation. Requires a running vLLM server.
Set VLLM_BASE_URL to enable.
"""

import pytest
import os
import time

from config import config


VLLM_BASE_URL = os.getenv("VLLM_BASE_URL")
VLLM_MODEL = os.getenv("VLLM_MODEL", "meta-llama/Llama-3.2-3B-Instruct")
HAS_VLLM = bool(VLLM_BASE_URL)

skip_if_no_vllm = pytest.mark.skipif(
    not HAS_VLLM, reason="VLLM_BASE_URL not set"
)


@pytest.fixture(scope="module")
def vllm_client():
    """Create an instrumented vLLM client via OpenAI-compat."""
    if not HAS_VLLM:
        pytest.skip("VLLM_BASE_URL not set")

    from fi_instrumentation import register
    try:
        from traceai_vllm import VLLMInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_vllm not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    VLLMInstrumentor().instrument(tracer_provider=tracer_provider)

    from openai import OpenAI

    client = OpenAI(
        base_url=f"{VLLM_BASE_URL}/v1",
        api_key="not-needed",  # vLLM doesn't need auth by default
    )

    yield client

    VLLMInstrumentor().uninstrument()


@skip_if_no_vllm
class TestVLLMChatCompletion:
    """Test vLLM chat completion."""

    def test_basic_chat(self, vllm_client):
        """Test basic chat completion."""
        response = vllm_client.chat.completions.create(
            model=VLLM_MODEL,
            messages=[
                {"role": "user", "content": "Say hello in one word."}
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        time.sleep(2)
        print(f"Response: {response.choices[0].message.content}")

    def test_streaming(self, vllm_client):
        """Test streaming chat completion."""
        stream = vllm_client.chat.completions.create(
            model=VLLM_MODEL,
            messages=[
                {"role": "user", "content": "Count from 1 to 5."}
            ],
            max_tokens=50,
            stream=True,
        )

        chunks = []
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed: {full_response}")

    def test_completion(self, vllm_client):
        """Test text completion (non-chat)."""
        response = vllm_client.completions.create(
            model=VLLM_MODEL,
            prompt="The capital of France is",
            max_tokens=10,
        )

        assert response.choices[0].text is not None
        print(f"Completion: {response.choices[0].text}")
