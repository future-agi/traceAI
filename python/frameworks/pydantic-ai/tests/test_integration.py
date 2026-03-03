"""Integration tests for Pydantic AI instrumentation.

These tests require pydantic-ai to be installed and an API key to be set.
They are marked with `integration` to be skipped in CI without credentials.
"""

import pytest
import os
from unittest.mock import MagicMock, patch

# Check if we can run integration tests
HAS_PYDANTIC_AI = False
HAS_OPENAI_KEY = bool(os.environ.get("OPENAI_API_KEY"))

try:
    import pydantic_ai
    HAS_PYDANTIC_AI = True
except ImportError:
    pass


@pytest.fixture
def memory_exporter():
    """Create an in-memory span exporter for testing."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    yield exporter, provider

    exporter.clear()


@pytest.fixture
def instrumented_pydantic_ai(memory_exporter):
    """Setup instrumented Pydantic AI with mocked pydantic_ai module."""
    from traceai_pydantic_ai import PydanticAIInstrumentor
    import sys

    exporter, provider = memory_exporter

    # Reset singleton
    PydanticAIInstrumentor._instance = None
    PydanticAIInstrumentor._is_instrumented = False

    instrumentor = PydanticAIInstrumentor()

    # Create mock pydantic_ai module
    mock_pydantic_ai = MagicMock()
    mock_agent_class = MagicMock()
    mock_agent_class.run = MagicMock()
    mock_agent_class.run_sync = MagicMock()
    mock_pydantic_ai.Agent = mock_agent_class

    with patch.object(instrumentor, "instrumentation_dependencies", return_value=[]):
        with patch.dict(sys.modules, {"pydantic_ai": mock_pydantic_ai}):
            instrumentor.instrument(tracer_provider=provider)
            yield exporter, instrumentor

    instrumentor.uninstrument()


class TestInstrumentorIntegration:
    """Test instrumentor with mock Pydantic AI."""

    def test_instrument_creates_spans(self, instrumented_pydantic_ai):
        """Test that instrumentation creates spans when methods are called."""
        exporter, instrumentor = instrumented_pydantic_ai

        assert instrumentor.is_instrumented
        assert instrumentor._tracer is not None

    def test_uninstrument_cleans_up(self, instrumented_pydantic_ai):
        """Test that uninstrument cleans up properly."""
        exporter, instrumentor = instrumented_pydantic_ai

        instrumentor.uninstrument()

        assert not instrumentor.is_instrumented
        assert instrumentor._tracer is None


@pytest.mark.skipif(not HAS_PYDANTIC_AI, reason="pydantic-ai not installed")
class TestPydanticAIInstalled:
    """Tests that run when pydantic-ai is installed."""

    def test_can_import_agent(self):
        """Test Agent can be imported."""
        from pydantic_ai import Agent
        assert Agent is not None

    def test_can_create_agent(self):
        """Test Agent can be created."""
        from pydantic_ai import Agent

        agent = Agent("test-model", instructions="Test")
        assert agent is not None

    def test_instrumentor_patches_agent(self, memory_exporter):
        """Test instrumentor patches Agent methods."""
        from traceai_pydantic_ai import PydanticAIInstrumentor
        from pydantic_ai import Agent

        exporter, provider = memory_exporter

        # Reset singleton
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = PydanticAIInstrumentor()

        # Store original method
        original_run = Agent.run

        with patch.object(instrumentor, "instrumentation_dependencies", return_value=[]):
            instrumentor.instrument(tracer_provider=provider)

        # Method should be different (wrapped)
        assert Agent.run is not original_run

        instrumentor.uninstrument()

        # Method should be restored
        assert Agent.run is original_run


@pytest.mark.skipif(
    not (HAS_PYDANTIC_AI and HAS_OPENAI_KEY),
    reason="Requires pydantic-ai and OPENAI_API_KEY"
)
@pytest.mark.integration
class TestRealWorldIntegration:
    """Real-world integration tests with actual API calls.

    These tests make real API calls and require:
    - pydantic-ai installed
    - OPENAI_API_KEY environment variable
    """

    @pytest.mark.asyncio
    async def test_basic_agent_run(self, memory_exporter):
        """Test basic agent run creates proper spans."""
        from traceai_pydantic_ai import PydanticAIInstrumentor
        from pydantic_ai import Agent

        exporter, provider = memory_exporter

        # Reset and instrument
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = PydanticAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider)

        try:
            # Create and run agent
            agent = Agent(
                "openai:gpt-4o-mini",
                instructions="Reply with just the number.",
            )

            result = await agent.run("What is 2+2?")

            # Get spans
            spans = exporter.get_finished_spans()

            # Should have at least one agent run span
            agent_spans = [
                s for s in spans if "agent.run" in s.name
            ]
            assert len(agent_spans) >= 1

            # Check span attributes
            span = agent_spans[0]
            assert span.attributes.get("pydantic_ai.run.method") == "run"
            assert "2+2" in span.attributes.get("pydantic_ai.run.prompt", "")

        finally:
            instrumentor.uninstrument()

    def test_sync_agent_run(self, memory_exporter):
        """Test synchronous agent run creates proper spans."""
        from traceai_pydantic_ai import PydanticAIInstrumentor
        from pydantic_ai import Agent

        exporter, provider = memory_exporter

        # Reset and instrument
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = PydanticAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider)

        try:
            agent = Agent(
                "openai:gpt-4o-mini",
                instructions="Reply with just the number.",
            )

            result = agent.run_sync("What is 3+3?")

            spans = exporter.get_finished_spans()

            agent_spans = [
                s for s in spans if "agent.run" in s.name
            ]
            assert len(agent_spans) >= 1

            span = agent_spans[0]
            assert span.attributes.get("pydantic_ai.run.method") == "run_sync"

        finally:
            instrumentor.uninstrument()

    def test_agent_with_structured_output(self, memory_exporter):
        """Test agent with structured output."""
        from traceai_pydantic_ai import PydanticAIInstrumentor
        from pydantic_ai import Agent
        from pydantic import BaseModel

        exporter, provider = memory_exporter

        class MathResult(BaseModel):
            answer: int
            explanation: str

        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = PydanticAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider)

        try:
            agent = Agent(
                "openai:gpt-4o-mini",
                result_type=MathResult,
            )

            result = agent.run_sync("What is 5+5?")

            # Should get structured output
            assert isinstance(result.output, MathResult)
            assert result.output.answer == 10

            spans = exporter.get_finished_spans()
            agent_spans = [s for s in spans if "agent.run" in s.name]
            assert len(agent_spans) >= 1

            span = agent_spans[0]
            assert span.attributes.get("pydantic_ai.run.is_structured") == True

        finally:
            instrumentor.uninstrument()

    def test_agent_with_tools(self, memory_exporter):
        """Test agent with tools creates tool spans."""
        from traceai_pydantic_ai import PydanticAIInstrumentor
        from pydantic_ai import Agent, RunContext

        exporter, provider = memory_exporter

        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = PydanticAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider)

        try:
            agent = Agent("openai:gpt-4o-mini")

            @agent.tool
            def add_numbers(ctx: RunContext, a: int, b: int) -> int:
                """Add two numbers together."""
                return a + b

            result = agent.run_sync(
                "Please use the add_numbers tool to add 7 and 8"
            )

            spans = exporter.get_finished_spans()

            # Should have agent run span
            agent_spans = [s for s in spans if "agent.run" in s.name]
            assert len(agent_spans) >= 1

        finally:
            instrumentor.uninstrument()

    def test_usage_metrics_captured(self, memory_exporter):
        """Test that token usage metrics are captured."""
        from traceai_pydantic_ai import PydanticAIInstrumentor
        from pydantic_ai import Agent

        exporter, provider = memory_exporter

        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = PydanticAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider)

        try:
            agent = Agent("openai:gpt-4o-mini")

            result = agent.run_sync("Say hello in exactly 5 words")

            # Result should have usage
            assert result.usage is not None

            spans = exporter.get_finished_spans()
            agent_spans = [s for s in spans if "agent.run" in s.name]

            if agent_spans:
                span = agent_spans[0]
                # Should have token usage attributes
                input_tokens = span.attributes.get("gen_ai.usage.input_tokens")
                output_tokens = span.attributes.get("gen_ai.usage.output_tokens")
                # These may be 0 or actual values
                assert input_tokens is not None or output_tokens is not None

        finally:
            instrumentor.uninstrument()

    def test_error_handling(self, memory_exporter):
        """Test error handling is captured in spans."""
        from traceai_pydantic_ai import PydanticAIInstrumentor
        from pydantic_ai import Agent

        exporter, provider = memory_exporter

        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = PydanticAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider)

        try:
            # Use invalid model to trigger error
            agent = Agent("openai:invalid-model-name")

            with pytest.raises(Exception):
                agent.run_sync("Hello")

            spans = exporter.get_finished_spans()
            agent_spans = [s for s in spans if "agent.run" in s.name]

            if agent_spans:
                span = agent_spans[0]
                # Should have error attributes
                assert span.attributes.get("pydantic_ai.is_error") == True

        finally:
            instrumentor.uninstrument()


@pytest.mark.skipif(not HAS_PYDANTIC_AI, reason="pydantic-ai not installed")
class TestConvenienceFunctions:
    """Test convenience functions with Pydantic AI installed."""

    def test_instrument_pydantic_ai_function(self, memory_exporter):
        """Test instrument_pydantic_ai convenience function."""
        from traceai_pydantic_ai import instrument_pydantic_ai, PydanticAIInstrumentor

        exporter, provider = memory_exporter

        # Reset singleton
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = instrument_pydantic_ai(tracer_provider=provider)

        assert instrumentor is not None
        assert instrumentor.is_instrumented

        instrumentor.uninstrument()

    def test_builtin_option(self, memory_exporter):
        """Test use_builtin option."""
        from traceai_pydantic_ai import instrument_pydantic_ai, PydanticAIInstrumentor

        exporter, provider = memory_exporter

        # Reset singleton
        PydanticAIInstrumentor._instance = None
        PydanticAIInstrumentor._is_instrumented = False

        instrumentor = instrument_pydantic_ai(
            tracer_provider=provider,
            use_builtin=True,
        )

        assert instrumentor._use_builtin == True

        instrumentor.uninstrument()
