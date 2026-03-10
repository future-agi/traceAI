"""
E2E Tests for Guardrails AI SDK Instrumentation

Tests Guardrails AI instrumentation using litellm's gemini provider.
"""

import os
import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_guardrails():
    """Set up Guardrails AI with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    # Guardrails 0.5+ uses litellm internally; set GEMINI_API_KEY for routing
    os.environ.setdefault("GEMINI_API_KEY", config.google_api_key)

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

        guard = Guard()

        result = guard(
            model="gemini/gemini-2.0-flash",
            messages=[
                {"role": "user", "content": "What is 2+2? Answer with just the number."}
            ],
            max_tokens=10,
        )

        assert result.validated_output is not None
        time.sleep(2)
        print(f"Guard result: {result.validated_output}")

    def test_guard_with_pydantic(self, setup_guardrails):
        """Test guard with Pydantic output model."""
        from pydantic import BaseModel
        from guardrails import Guard

        class CityInfo(BaseModel):
            name: str
            country: str

        guard = Guard.for_pydantic(output_class=CityInfo)

        result = guard(
            model="gemini/gemini-2.0-flash",
            messages=[
                {"role": "user", "content": "Tell me about Paris, the capital of France."}
            ],
            max_tokens=100,
        )

        assert result.validated_output is not None
        if isinstance(result.validated_output, CityInfo):
            assert "Paris" in result.validated_output.name
        print(f"Pydantic guard result: {result.validated_output}")
