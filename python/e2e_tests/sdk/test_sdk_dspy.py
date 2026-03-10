"""
E2E Tests for DSPy SDK Instrumentation

Tests DSPy instrumentation using Google's OpenAI-compatible endpoint.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_dspy():
    """Set up DSPy with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_dspy import DSPyInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_dspy not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    DSPyInstrumentor().instrument(tracer_provider=tracer_provider)

    import dspy

    lm = dspy.LM(
        f"openai/{config.google_model}",
        api_base=config.google_openai_base_url,
        api_key=config.google_api_key,
    )
    dspy.configure(lm=lm)

    yield

    DSPyInstrumentor().uninstrument()


@skip_if_no_google
class TestDSPyPredict:
    """Test DSPy Predict module."""

    def test_basic_predict(self, setup_dspy):
        """Test basic prediction."""
        import dspy

        predict = dspy.Predict("question -> answer")
        result = predict(question="What is 2+2?")

        assert result.answer is not None
        assert "4" in result.answer
        time.sleep(2)
        print(f"Predict result: {result.answer}")

    def test_chain_of_thought(self, setup_dspy):
        """Test Chain of Thought module."""
        import dspy

        cot = dspy.ChainOfThought("question -> answer")
        result = cot(question="What is the capital of France?")

        assert result.answer is not None
        assert "Paris" in result.answer
        print(f"CoT result: {result.answer}")

    def test_typed_predictor(self, setup_dspy):
        """Test typed predictor with signature."""
        import dspy

        class QA(dspy.Signature):
            """Answer the question briefly."""
            question: str = dspy.InputField()
            answer: str = dspy.OutputField()

        predict = dspy.Predict(QA)
        result = predict(question="What color is the sky?")

        assert result.answer is not None
        print(f"Typed result: {result.answer}")


@skip_if_no_google
class TestDSPyModule:
    """Test DSPy custom modules."""

    def test_custom_module(self, setup_dspy):
        """Test custom DSPy module."""
        import dspy

        class SimpleQA(dspy.Module):
            def __init__(self):
                super().__init__()
                self.predict = dspy.Predict("question -> answer")

            def forward(self, question):
                return self.predict(question=question)

        qa = SimpleQA()
        result = qa(question="What is 3 * 7?")

        assert result.answer is not None
        assert "21" in result.answer
        print(f"Module result: {result.answer}")


@skip_if_no_google
class TestDSPyErrorHandling:
    """Test error handling."""

    def test_empty_input(self, setup_dspy):
        """Test handling of empty input."""
        import dspy

        predict = dspy.Predict("question -> answer")
        # DSPy should still attempt but may produce empty/error
        result = predict(question="")
        # Just verify it doesn't crash
        assert result is not None
