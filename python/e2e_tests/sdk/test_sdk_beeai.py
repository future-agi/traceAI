"""
E2E Tests for BeeAI SDK Instrumentation

Tests BeeAI (IBM) instrumentation. Requires BeeAI/OpenInference packages.
Attempts to use Google endpoint via OpenAI-compat where possible.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_beeai():
    """Set up BeeAI with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    try:
        from fi_instrumentation import register
        from traceai_beeai import BeeAIInstrumentorWrapper

        tracer_provider = register(
            project_name=config.project_name,
            project_version_name=config.project_version_name,
            project_type=config.project_type,
            verbose=False,
        )

        BeeAIInstrumentorWrapper().instrument(tracer_provider=tracer_provider)

        yield

        BeeAIInstrumentorWrapper().uninstrument()
    except (ImportError, AttributeError):
        pytest.skip("BeeAI or OpenInference packages not installed")


@skip_if_no_google
class TestBeeAIAgent:
    """Test BeeAI agent operations."""

    def test_simple_agent(self, setup_beeai):
        """Test simple BeeAI agent run."""
        try:
            from beeai import BeeAgent
            from beeai.llms.openai import OpenAIChatLLM

            llm = OpenAIChatLLM(
                model_id=config.google_model,
                base_url=config.google_openai_base_url,
                api_key=config.google_api_key,
            )

            agent = BeeAgent(llm=llm)

            response = agent.run("What is 2+2? Answer with just the number.")

            assert response is not None
            assert "4" in str(response.result.text)
            time.sleep(2)
            print(f"BeeAI result: {response.result.text}")

        except (ImportError, AttributeError) as e:
            pytest.skip(f"BeeAI API not compatible: {e}")
