"""
E2E Test: Multi-Model Pipeline Use Case

Simulates a pipeline using multiple LLM providers.
"""

import pytest
import os
import time
from typing import Dict, Any

from config import config, skip_if_no_openai, skip_if_no_anthropic


@pytest.fixture(scope="module")
def setup_multi_model():
    """Set up multiple providers with instrumentation."""
    # Set API keys
    if config.has_openai():
        os.environ["OPENAI_API_KEY"] = config.openai_api_key
    if config.has_anthropic():
        os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key

    from fi_instrumentation import register
    from traceai_openai import OpenAIInstrumentor
    from traceai_anthropic import AnthropicInstrumentor

    tracer_provider = register(
        project_name="e2e_multi_model_usecase",
        project_version_name="1.0.0",
        verbose=False,
    )

    if config.has_openai():
        OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
    if config.has_anthropic():
        AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)

    yield tracer_provider

    if config.has_openai():
        OpenAIInstrumentor().uninstrument()
    if config.has_anthropic():
        AnthropicInstrumentor().uninstrument()


@pytest.mark.skipif(
    not (config.has_openai() and config.has_anthropic()),
    reason="Both OpenAI and Anthropic keys required"
)
class TestMultiModelPipeline:
    """Test multi-model pipeline scenarios."""

    def test_draft_and_refine(self, setup_multi_model):
        """Use one model to draft, another to refine."""
        from openai import OpenAI
        from anthropic import Anthropic

        openai_client = OpenAI()
        anthropic_client = Anthropic()

        # Step 1: OpenAI generates initial draft
        draft_response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": "Write a short paragraph about the benefits of exercise."
                }
            ],
            max_tokens=150,
        )
        draft = draft_response.choices[0].message.content
        print(f"Draft (OpenAI): {draft}")

        # Step 2: Anthropic refines the draft
        refine_response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": f"Improve this paragraph to be more engaging and concise:\n\n{draft}"
                }
            ],
        )
        refined = refine_response.content[0].text
        print(f"Refined (Anthropic): {refined}")

        assert len(draft) > 0
        assert len(refined) > 0

        time.sleep(2)

    def test_cross_check(self, setup_multi_model):
        """Use two models to cross-check answers."""
        from openai import OpenAI
        from anthropic import Anthropic

        openai_client = OpenAI()
        anthropic_client = Anthropic()

        question = "What is the capital of Australia?"

        # Get answer from OpenAI
        openai_response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": question}],
            max_tokens=50,
        )
        openai_answer = openai_response.choices[0].message.content
        print(f"OpenAI: {openai_answer}")

        # Get answer from Anthropic
        anthropic_response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=50,
            messages=[{"role": "user", "content": question}],
        )
        anthropic_answer = anthropic_response.content[0].text
        print(f"Anthropic: {anthropic_answer}")

        # Both should mention Canberra
        assert "canberra" in openai_answer.lower()
        assert "canberra" in anthropic_answer.lower()

        time.sleep(2)

    def test_specialized_tasks(self, setup_multi_model):
        """Use different models for specialized tasks."""
        from openai import OpenAI
        from anthropic import Anthropic

        openai_client = OpenAI()
        anthropic_client = Anthropic()

        # Task 1: Code generation (OpenAI)
        code_response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": "Write a Python function to calculate factorial."
                }
            ],
            max_tokens=200,
        )
        code = code_response.choices[0].message.content
        print(f"Generated code: {code[:100]}...")

        # Task 2: Explain code (Anthropic)
        explain_response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": f"Explain this code in simple terms:\n\n{code}"
                }
            ],
        )
        explanation = explain_response.content[0].text
        print(f"Explanation: {explanation[:100]}...")

        assert "def" in code or "function" in code.lower()
        assert len(explanation) > 0

        time.sleep(2)


@skip_if_no_openai
class TestModelComparison:
    """Test comparing different models from same provider."""

    def test_compare_openai_models(self, setup_multi_model):
        """Compare responses from different OpenAI models."""
        from openai import OpenAI

        client = OpenAI()
        prompt = "Explain quantum computing in one sentence."

        models = ["gpt-4o-mini", "gpt-4o"]
        results = {}

        for model in models:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=100,
                )
                results[model] = response.choices[0].message.content
                print(f"{model}: {results[model]}")
            except Exception as e:
                print(f"{model}: Error - {e}")

        # At least one model should work
        assert len(results) > 0

        time.sleep(2)


@skip_if_no_openai
class TestFallbackScenario:
    """Test fallback between providers."""

    def test_model_fallback(self, setup_multi_model):
        """Simulate fallback when primary model fails."""
        from openai import OpenAI

        client = OpenAI()

        # Try primary model
        primary_failed = False
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=10,
            )
            result = response.choices[0].message.content
        except Exception as e:
            primary_failed = True
            # Fallback to different model
            response = client.chat.completions.create(
                model="gpt-4o-mini",  # Using same model as fallback for test
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=10,
            )
            result = response.choices[0].message.content

        assert result is not None
        print(f"Result (fallback: {primary_failed}): {result}")

        time.sleep(2)
