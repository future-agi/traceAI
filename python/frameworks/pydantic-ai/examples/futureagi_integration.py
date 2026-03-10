"""FutureAGI Platform integration example.

This example demonstrates how to use traceai-pydantic-ai with
the FutureAGI observability platform for production monitoring.
"""

import os
from pydantic import BaseModel, Field

# Check for FutureAGI API key
if not os.environ.get("FI_API_KEY"):
    print("Note: Set FI_API_KEY environment variable for FutureAGI integration")
    print("Running in demo mode with console output...\n")


class TaskResult(BaseModel):
    """Structured task result."""

    task_id: str = Field(description="Unique task identifier")
    status: str = Field(description="Task status: success, failed, pending")
    result: str = Field(description="Task result or error message")
    duration_seconds: float = Field(description="Task execution time")


def setup_futureagi_tracing():
    """Setup tracing with FutureAGI platform."""
    api_key = os.environ.get("FI_API_KEY")

    if api_key:
        try:
            from fi_instrumentation import register
            from fi_instrumentation.fi_types import ProjectType

            trace_provider = register(
                api_key=api_key,
                project_type=ProjectType.OBSERVE,
                project_name="pydantic-ai-demo",
            )
            print("Connected to FutureAGI platform")
            return trace_provider
        except ImportError:
            print("fi-instrumentation not installed, using console output")

    # Fallback to console exporter
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider()
    processor = SimpleSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    return provider


def main():
    # Setup tracing (FutureAGI or console fallback)
    provider = setup_futureagi_tracing()

    # Initialize Pydantic AI instrumentation
    from traceai_pydantic_ai import PydanticAIInstrumentor

    PydanticAIInstrumentor().instrument(tracer_provider=provider)

    # Import after instrumentation
    from pydantic_ai import Agent, RunContext

    # Create production-ready agent
    agent = Agent(
        "openai:gpt-4o-mini",
        result_type=TaskResult,
        instructions=(
            "You are a task processing assistant. "
            "Process tasks and return structured results. "
            "Always include a unique task_id, status, result, and duration."
        ),
    )

    # Processing tool
    @agent.tool
    def process_data(ctx: RunContext, data: str) -> str:
        """Process incoming data.

        Args:
            data: Data to process.

        Returns:
            Processing result.
        """
        # Simulate processing
        import time

        start = time.time()
        time.sleep(0.1)  # Simulate work
        duration = time.time() - start

        return f"Processed '{data}' in {duration:.3f}s"

    # Run task
    print("Processing task...")
    result = agent.run_sync(
        "Process the following data: 'user_registration_event_12345'"
    )

    task_result = result.output
    print(f"\nTask ID: {task_result.task_id}")
    print(f"Status: {task_result.status}")
    print(f"Result: {task_result.result}")
    print(f"Duration: {task_result.duration_seconds}s")

    if result.usage:
        print(f"\nToken Usage:")
        print(f"  Input: {result.usage.request_tokens}")
        print(f"  Output: {result.usage.response_tokens}")
        print(f"  Total: {result.usage.total_tokens}")

    print("\nTraces sent to FutureAGI platform (or console if no API key)")


if __name__ == "__main__":
    main()
