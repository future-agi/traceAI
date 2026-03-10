"""
E2E Tests for Haystack SDK Instrumentation

Tests Haystack instrumentation using Google's OpenAI-compatible endpoint.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_haystack():
    """Set up Haystack with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_haystack import HaystackInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_haystack not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    HaystackInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    HaystackInstrumentor().uninstrument()


@skip_if_no_google
class TestHaystackGenerator:
    """Test Haystack OpenAI generator."""

    def test_basic_generation(self, setup_haystack):
        """Test basic text generation."""
        from haystack.components.generators import OpenAIGenerator

        generator = OpenAIGenerator(
            model=config.google_model,
            api_base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
            generation_kwargs={"max_tokens": 50},
        )

        result = generator.run(prompt="Say 'Hello E2E Test' in exactly 3 words.")

        assert "replies" in result
        assert len(result["replies"]) > 0
        assert result["replies"][0] is not None
        time.sleep(2)
        print(f"Response: {result['replies'][0]}")

    def test_chat_generation(self, setup_haystack):
        """Test chat generation."""
        from haystack.components.generators.chat import OpenAIChatGenerator
        from haystack.dataclasses import ChatMessage

        generator = OpenAIChatGenerator(
            model=config.google_model,
            api_base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
            generation_kwargs={"max_tokens": 50},
        )

        messages = [
            ChatMessage.from_system("You are a helpful assistant that responds briefly."),
            ChatMessage.from_user("What is 2+2?"),
        ]

        result = generator.run(messages=messages)

        assert "replies" in result
        assert len(result["replies"]) > 0
        assert "4" in result["replies"][0].text
        print(f"Chat response: {result['replies'][0].text}")


@skip_if_no_google
class TestHaystackPipeline:
    """Test Haystack pipeline with tracing."""

    def test_simple_pipeline(self, setup_haystack):
        """Test a simple pipeline."""
        from haystack import Pipeline
        from haystack.components.generators import OpenAIGenerator
        from haystack.components.builders import PromptBuilder

        prompt_template = "Answer the question briefly: {{question}}"

        pipeline = Pipeline()
        pipeline.add_component("prompt_builder", PromptBuilder(template=prompt_template))
        pipeline.add_component(
            "llm",
            OpenAIGenerator(
                model=config.google_model,
                api_base_url=config.google_openai_base_url,
                api_key=config.google_api_key,
                generation_kwargs={"max_tokens": 50},
            ),
        )
        pipeline.connect("prompt_builder", "llm")

        result = pipeline.run({"prompt_builder": {"question": "What is the capital of France?"}})

        assert "llm" in result
        assert len(result["llm"]["replies"]) > 0
        assert "Paris" in result["llm"]["replies"][0]
        print(f"Pipeline result: {result['llm']['replies'][0]}")

    def test_multi_step_pipeline(self, setup_haystack):
        """Test multi-step pipeline."""
        from haystack import Pipeline
        from haystack.components.generators import OpenAIGenerator
        from haystack.components.builders import PromptBuilder

        # Step 1: Generate a fact
        fact_template = "State one interesting fact about: {{topic}}"
        # Step 2: Summarize
        summary_template = "Summarize this in exactly one sentence: {{replies[0]}}"

        pipeline = Pipeline()
        pipeline.add_component("fact_prompt", PromptBuilder(template=fact_template))
        pipeline.add_component(
            "fact_llm",
            OpenAIGenerator(
                model=config.google_model,
                api_base_url=config.google_openai_base_url,
                api_key=config.google_api_key,
                generation_kwargs={"max_tokens": 100},
            ),
        )
        pipeline.add_component("summary_prompt", PromptBuilder(template=summary_template))
        pipeline.add_component(
            "summary_llm",
            OpenAIGenerator(
                model=config.google_model,
                api_base_url=config.google_openai_base_url,
                api_key=config.google_api_key,
                generation_kwargs={"max_tokens": 50},
            ),
        )

        pipeline.connect("fact_prompt", "fact_llm")
        pipeline.connect("fact_llm.replies", "summary_prompt.replies")
        pipeline.connect("summary_prompt", "summary_llm")

        result = pipeline.run({"fact_prompt": {"topic": "the moon"}})

        assert "summary_llm" in result
        assert len(result["summary_llm"]["replies"]) > 0
        print(f"Summary: {result['summary_llm']['replies'][0]}")
