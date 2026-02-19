"""
E2E Tests for LiteLLM Instrumentation

Tests LiteLLM's unified interface to multiple LLM providers.
"""

import pytest
import os
import time
from typing import Dict, Any

from config import config, skip_if_no_openai, skip_if_no_anthropic


@pytest.fixture(scope="module")
def setup_litellm():
    """Set up LiteLLM with instrumentation."""
    # Set API keys
    if config.has_openai():
        os.environ["OPENAI_API_KEY"] = config.openai_api_key
    if config.has_anthropic():
        os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    if config.has_groq():
        os.environ["GROQ_API_KEY"] = config.groq_api_key

    # Import and instrument
    from fi_instrumentation import register
    from traceai_litellm import LiteLLMInstrumentor

    # Register tracer
    tracer_provider = register(
        project_name="e2e_test_litellm",
        project_version_name="1.0.0",
    )

    # Instrument LiteLLM
    LiteLLMInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    # Cleanup
    LiteLLMInstrumentor().uninstrument()


@skip_if_no_openai
class TestLiteLLMOpenAI:
    """Test LiteLLM with OpenAI backend."""

    def test_openai_completion(self, setup_litellm):
        """Test OpenAI via LiteLLM."""
        import litellm

        response = litellm.completion(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "Say hello in one word."}
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        time.sleep(2)
        print(f"Response: {response.choices[0].message.content}")
        print(f"Tokens: {response.usage.total_tokens}")

    def test_openai_streaming(self, setup_litellm):
        """Test OpenAI streaming via LiteLLM."""
        import litellm

        response = litellm.completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Count 1 to 3."}],
            max_tokens=30,
            stream=True,
        )

        chunks = []
        for chunk in response:
            if chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)

        assert len(chunks) > 0
        print(f"Streamed: {''.join(chunks)}")


@skip_if_no_anthropic
class TestLiteLLMAnthropic:
    """Test LiteLLM with Anthropic backend."""

    def test_anthropic_completion(self, setup_litellm):
        """Test Anthropic via LiteLLM."""
        import litellm

        response = litellm.completion(
            model="claude-3-5-haiku-latest",
            messages=[
                {"role": "user", "content": "Say hello briefly."}
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        print(f"Response: {response.choices[0].message.content}")


class TestLiteLLMMultiProvider:
    """Test LiteLLM with multiple providers."""

    @pytest.mark.skipif(
        not (config.has_openai() and config.has_anthropic()),
        reason="Both OpenAI and Anthropic keys required"
    )
    def test_provider_switching(self, setup_litellm):
        """Test switching between providers."""
        import litellm

        # OpenAI
        openai_response = litellm.completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "What is 2+2?"}],
            max_tokens=10,
        )

        # Anthropic
        anthropic_response = litellm.completion(
            model="claude-3-5-haiku-latest",
            messages=[{"role": "user", "content": "What is 2+2?"}],
            max_tokens=10,
        )

        assert openai_response.choices[0].message.content is not None
        assert anthropic_response.choices[0].message.content is not None

        print(f"OpenAI: {openai_response.choices[0].message.content}")
        print(f"Anthropic: {anthropic_response.choices[0].message.content}")


@skip_if_no_openai
class TestLiteLLMAsync:
    """Test async LiteLLM operations."""

    @pytest.mark.asyncio
    async def test_async_completion(self, setup_litellm):
        """Test async completion."""
        import litellm

        response = await litellm.acompletion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say async briefly."}],
            max_tokens=10,
        )

        assert response.choices[0].message.content is not None


@skip_if_no_openai
class TestLiteLLMEmbeddings:
    """Test LiteLLM embeddings."""

    def test_embeddings(self, setup_litellm):
        """Test embedding creation."""
        import litellm

        response = litellm.embedding(
            model="text-embedding-3-small",
            input=["Hello, world!"],
        )

        assert len(response.data) > 0
        assert len(response.data[0]["embedding"]) > 0
        print(f"Embedding dimensions: {len(response.data[0]['embedding'])}")


@skip_if_no_openai
class TestLiteLLMFallback:
    """Test LiteLLM fallback and routing features."""

    def test_model_fallback(self, setup_litellm):
        """Test model fallback."""
        import litellm
        from litellm import Router

        # Configure router with fallback
        router = Router(
            model_list=[
                {
                    "model_name": "primary",
                    "litellm_params": {
                        "model": "gpt-4o-mini",
                    },
                },
            ],
            fallbacks=[{"primary": ["gpt-4o-mini"]}],
        )

        response = router.completion(
            model="primary",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=10,
        )

        assert response.choices[0].message.content is not None
