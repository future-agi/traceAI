"""Real-world scenario tests for Pydantic AI instrumentation.

These tests simulate common real-world usage patterns without requiring
actual API calls, using mocks to verify correct instrumentation behavior.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio


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

    yield exporter, provider

    exporter.clear()


class TestChatbotScenario:
    """Test chatbot/conversation scenario."""

    @pytest.mark.asyncio
    async def test_multi_turn_conversation(self, memory_exporter):
        """Test multi-turn conversation tracing."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        # Simulate conversation
        conversation = [
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]

        mock_result = MagicMock()
        mock_result.usage = MagicMock(
            request_tokens=50,
            response_tokens=30,
            total_tokens=80,
        )
        mock_result.output = "I'm doing well!"

        async def mock_run(self, prompt, message_history=None):
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = "Be friendly and helpful"
        mock_agent.result_type = None

        wrapped = wrap_agent_run(mock_run, tracer, "run")

        # Simulate conversation turns
        for i in range(3):
            await wrapped(mock_agent, f"Turn {i+1}", message_history=conversation[:i])

        spans = exporter.get_finished_spans()
        assert len(spans) == 3

    def test_conversation_with_context(self, memory_exporter):
        """Test conversation with context/dependencies."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        mock_result = MagicMock()
        mock_result.usage = None
        mock_result.output = "Response with context"

        def mock_run_sync(self, prompt):
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "anthropic:claude-3-sonnet"
        mock_agent.instructions = None
        mock_agent.result_type = None

        wrapped = wrap_agent_run(mock_run_sync, tracer, "run_sync")

        result = wrapped(mock_agent, "Hello with context")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes.get("gen_ai.system") == "anthropic"


class TestRAGPipelineScenario:
    """Test RAG (Retrieval Augmented Generation) scenario."""

    @pytest.mark.asyncio
    async def test_rag_with_tools(self, memory_exporter):
        """Test RAG pipeline with search tool."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run, wrap_tool_function

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        # Create search tool
        def search_documents(ctx, query: str) -> str:
            return f"Found documents for: {query}"

        wrapped_tool = wrap_tool_function(
            search_documents,
            tracer,
            "search_documents",
            "Search document database",
        )

        # Execute tool
        result = wrapped_tool(MagicMock(), "machine learning")
        assert "Found documents" in result

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert "search_documents" in spans[0].name

    @pytest.mark.asyncio
    async def test_rag_with_multiple_tools(self, memory_exporter):
        """Test RAG with multiple tools in sequence."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        # Retriever tool
        def retrieve(ctx, query: str) -> list:
            return ["doc1", "doc2", "doc3"]

        # Reranker tool
        def rerank(ctx, docs: list, query: str) -> list:
            return docs[:2]

        wrapped_retrieve = wrap_tool_function(retrieve, tracer, "retrieve")
        wrapped_rerank = wrap_tool_function(rerank, tracer, "rerank")

        # Execute RAG pipeline
        docs = wrapped_retrieve(MagicMock(), "AI trends")
        ranked = wrapped_rerank(MagicMock(), docs, "AI trends")

        assert len(ranked) == 2

        spans = exporter.get_finished_spans()
        assert len(spans) == 2


class TestStructuredOutputScenario:
    """Test structured output scenarios."""

    @pytest.mark.asyncio
    async def test_structured_output_extraction(self, memory_exporter):
        """Test structured data extraction."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        # Simulate structured output
        class PersonInfo:
            name: str
            age: int
            occupation: str

        mock_output = MagicMock()
        mock_output.name = "Alice"
        mock_output.age = 30
        mock_output.occupation = "Engineer"

        mock_result = MagicMock()
        mock_result.usage = None
        mock_result.output = mock_output

        async def mock_run(self, prompt):
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = None
        mock_agent.result_type = PersonInfo

        wrapped = wrap_agent_run(mock_run, tracer, "run")

        result = await wrapped(mock_agent, "Extract person info")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes.get("pydantic_ai.run.is_structured") == True

    @pytest.mark.asyncio
    async def test_json_mode_extraction(self, memory_exporter):
        """Test JSON mode output."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        mock_result = MagicMock()
        mock_result.usage = None
        mock_result.output = {"status": "success", "data": [1, 2, 3]}

        async def mock_run(self, prompt):
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = None
        mock_agent.result_type = dict

        wrapped = wrap_agent_run(mock_run, tracer, "run")

        result = await wrapped(mock_agent, "Return JSON data")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1


class TestToolCallingScenario:
    """Test tool/function calling scenarios."""

    @pytest.mark.asyncio
    async def test_async_tool_execution(self, memory_exporter):
        """Test async tool execution."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        async def async_api_call(ctx, endpoint: str) -> dict:
            await asyncio.sleep(0.01)  # Simulate API delay
            return {"endpoint": endpoint, "status": "ok"}

        wrapped = wrap_tool_function(async_api_call, tracer, "api_call")

        result = await wrapped(MagicMock(), "/users")

        assert result["status"] == "ok"

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes.get("pydantic_ai.tool.is_error") == False

    def test_tool_with_complex_args(self, memory_exporter):
        """Test tool with complex arguments."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        def complex_tool(ctx, config: dict, items: list, enabled: bool = True) -> str:
            return f"Processed {len(items)} items with config {config}"

        wrapped = wrap_tool_function(complex_tool, tracer, "complex_tool")

        result = wrapped(
            MagicMock(),
            config={"mode": "fast", "limit": 100},
            items=["a", "b", "c"],
            enabled=True,
        )

        assert "Processed 3 items" in result

        spans = exporter.get_finished_spans()
        assert len(spans) == 1

    def test_tool_error_recovery(self, memory_exporter):
        """Test tool error handling."""
        from traceai_pydantic_ai._agent_wrapper import wrap_tool_function

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        call_count = 0

        def flaky_tool(ctx, attempt: int) -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("Temporary failure")
            return "Success"

        wrapped = wrap_tool_function(flaky_tool, tracer, "flaky_tool")

        # First call fails
        with pytest.raises(ValueError):
            wrapped(MagicMock(), 1)

        # Second call succeeds
        result = wrapped(MagicMock(), 2)
        assert result == "Success"

        spans = exporter.get_finished_spans()
        assert len(spans) == 2

        # First span should have error
        assert spans[0].attributes.get("pydantic_ai.tool.is_error") == True
        # Second span should succeed
        assert spans[1].attributes.get("pydantic_ai.tool.is_error") == False


class TestMultiModelScenario:
    """Test multi-model scenarios."""

    @pytest.mark.asyncio
    async def test_model_routing(self, memory_exporter):
        """Test routing to different models."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        mock_result = MagicMock()
        mock_result.usage = None
        mock_result.output = "Response"

        async def mock_run(self, prompt):
            return mock_result

        models = ["openai:gpt-4o", "anthropic:claude-3-sonnet", "google-gla:gemini-pro"]

        for model in models:
            mock_agent = MagicMock()
            mock_agent.model = model
            mock_agent.instructions = None
            mock_agent.result_type = None

            wrapped = wrap_agent_run(mock_run, tracer, "run")
            await wrapped(mock_agent, "Hello")

        spans = exporter.get_finished_spans()
        assert len(spans) == 3

        providers = [s.attributes.get("gen_ai.system") for s in spans]
        assert "openai" in providers
        assert "anthropic" in providers
        assert "google-gla" in providers

    @pytest.mark.asyncio
    async def test_fallback_model(self, memory_exporter):
        """Test fallback to secondary model on error."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        call_count = 0

        async def mock_run_with_fallback(self, prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Primary model failed")
            mock_result = MagicMock()
            mock_result.usage = None
            mock_result.output = "Fallback response"
            return mock_result

        # Primary model
        primary_agent = MagicMock()
        primary_agent.model = "openai:gpt-4o"
        primary_agent.instructions = None
        primary_agent.result_type = None

        # Fallback model
        fallback_agent = MagicMock()
        fallback_agent.model = "anthropic:claude-3-haiku"
        fallback_agent.instructions = None
        fallback_agent.result_type = None

        wrapped = wrap_agent_run(mock_run_with_fallback, tracer, "run")

        # Primary fails
        with pytest.raises(Exception):
            await wrapped(primary_agent, "Hello")

        # Fallback succeeds
        result = await wrapped(fallback_agent, "Hello")

        spans = exporter.get_finished_spans()
        assert len(spans) == 2

        # First span should have error
        assert spans[0].attributes.get("pydantic_ai.is_error") == True
        # Second span should succeed
        assert spans[1].attributes.get("pydantic_ai.is_error") == False


class TestCostTrackingScenario:
    """Test cost tracking scenarios."""

    @pytest.mark.asyncio
    async def test_token_usage_tracking(self, memory_exporter):
        """Test token usage is properly tracked."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        mock_usage = MagicMock()
        mock_usage.request_tokens = 150
        mock_usage.response_tokens = 75
        mock_usage.total_tokens = 225

        mock_result = MagicMock()
        mock_result.usage = mock_usage
        mock_result.output = "Response text"

        async def mock_run(self, prompt):
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = None
        mock_agent.result_type = None

        wrapped = wrap_agent_run(mock_run, tracer, "run")
        await wrapped(mock_agent, "Hello")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1

        span = spans[0]
        assert span.attributes.get("gen_ai.usage.input_tokens") == 150
        assert span.attributes.get("gen_ai.usage.output_tokens") == 75

    @pytest.mark.asyncio
    async def test_cumulative_usage(self, memory_exporter):
        """Test cumulative token usage across multiple calls."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        call_num = 0

        async def mock_run(self, prompt):
            nonlocal call_num
            call_num += 1

            mock_usage = MagicMock()
            mock_usage.request_tokens = 100 * call_num
            mock_usage.response_tokens = 50 * call_num
            mock_usage.total_tokens = 150 * call_num

            mock_result = MagicMock()
            mock_result.usage = mock_usage
            mock_result.output = f"Response {call_num}"
            return mock_result

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.instructions = None
        mock_agent.result_type = None

        wrapped = wrap_agent_run(mock_run, tracer, "run")

        # Make 3 calls
        for i in range(3):
            await wrapped(mock_agent, f"Prompt {i+1}")

        spans = exporter.get_finished_spans()
        assert len(spans) == 3

        # Calculate total
        total_input = sum(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in spans)
        total_output = sum(s.attributes.get("gen_ai.usage.output_tokens", 0) for s in spans)

        assert total_input == 100 + 200 + 300  # 600
        assert total_output == 50 + 100 + 150  # 300


class TestErrorScenarios:
    """Test error handling scenarios."""

    @pytest.mark.asyncio
    async def test_rate_limit_error(self, memory_exporter):
        """Test rate limit error handling."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        class RateLimitError(Exception):
            pass

        async def mock_run(self, prompt):
            raise RateLimitError("Rate limit exceeded")

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"

        wrapped = wrap_agent_run(mock_run, tracer, "run")

        with pytest.raises(RateLimitError):
            await wrapped(mock_agent, "Hello")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1

        span = spans[0]
        assert span.attributes.get("pydantic_ai.is_error") == True
        assert span.attributes.get("pydantic_ai.error.type") == "RateLimitError"
        assert "Rate limit" in span.attributes.get("pydantic_ai.error.message", "")

    @pytest.mark.asyncio
    async def test_timeout_error(self, memory_exporter):
        """Test timeout error handling."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        async def mock_run(self, prompt):
            raise TimeoutError("Request timed out")

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"

        wrapped = wrap_agent_run(mock_run, tracer, "run")

        with pytest.raises(TimeoutError):
            await wrapped(mock_agent, "Hello")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1

        span = spans[0]
        assert span.attributes.get("pydantic_ai.is_error") == True
        assert span.attributes.get("pydantic_ai.error.type") == "TimeoutError"

    @pytest.mark.asyncio
    async def test_validation_error(self, memory_exporter):
        """Test validation error handling."""
        from traceai_pydantic_ai._agent_wrapper import wrap_agent_run

        exporter, provider = memory_exporter
        tracer = provider.get_tracer("test")

        class ValidationError(Exception):
            pass

        async def mock_run(self, prompt):
            raise ValidationError("Output validation failed")

        mock_agent = MagicMock()
        mock_agent.model = "openai:gpt-4o"
        mock_agent.result_type = dict

        wrapped = wrap_agent_run(mock_run, tracer, "run")

        with pytest.raises(ValidationError):
            await wrapped(mock_agent, "Generate invalid output")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1

        span = spans[0]
        assert span.attributes.get("pydantic_ai.is_error") == True
