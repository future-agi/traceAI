"""
E2E Tests for Guardrails AI SDK Instrumentation

Tests Guardrails AI instrumentation using Google's OpenAI-compatible endpoint.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_guardrails():
    """Set up Guardrails AI with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_guardrails import GuardrailsInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_guardrails not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    GuardrailsInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    GuardrailsInstrumentor().uninstrument()


@skip_if_no_google
class TestGuardrailsBasic:
    """Test Guardrails AI basic operations."""

    def test_simple_guard(self, setup_guardrails):
        """Test simple guard with string validation."""
        from guardrails import Guard
        from openai import OpenAI

        client = OpenAI(
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        guard = Guard()

        result = guard(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "What is 2+2? Answer with just the number."}
            ],
            client=client,
            max_tokens=10,
        )

        assert result.validated_output is not None
        time.sleep(2)
        print(f"Guard result: {result.validated_output}")

    def test_guard_with_pydantic(self, setup_guardrails):
        """Test guard with Pydantic output model."""
        from pydantic import BaseModel
        from guardrails import Guard
        from openai import OpenAI

        class CityInfo(BaseModel):
            name: str
            country: str

        client = OpenAI(
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        guard = Guard.for_pydantic(output_class=CityInfo)

        result = guard(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "Tell me about Paris, the capital of France."}
            ],
            client=client,
            max_tokens=100,
        )

        assert result.validated_output is not None
        if isinstance(result.validated_output, CityInfo):
            assert "Paris" in result.validated_output.name
        print(f"Pydantic guard result: {result.validated_output}")
