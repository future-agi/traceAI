"""Basic example of Claude Agent SDK instrumentation.

This example demonstrates how to instrument the Claude Agent SDK
with OpenTelemetry tracing using TraceAI.
"""

import asyncio

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, BatchSpanProcessor

# Set up OpenTelemetry with console export
provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Initialize TraceAI instrumentation BEFORE importing claude_agent_sdk
from traceai_claude_agent_sdk import ClaudeAgentInstrumentor

ClaudeAgentInstrumentor().instrument(tracer_provider=provider)

# Now import and use the Claude Agent SDK
# Note: Requires claude-agent-sdk to be installed and configured with API key
try:
    from claude_agent_sdk import query, ClaudeAgentOptions

    async def main():
        """Run a simple agent query."""
        print("Running Claude Agent with tracing enabled...\n")

        async for message in query(
            prompt="List the files in the current directory",
            options=ClaudeAgentOptions(
                allowed_tools=["Bash"],
                max_turns=3,
            )
        ):
            msg_type = type(message).__name__
            print(f"[{msg_type}] ", end="")

            if hasattr(message, "content"):
                content = message.content
                if isinstance(content, str):
                    print(content[:200])
                elif isinstance(content, list):
                    for block in content:
                        if hasattr(block, "text"):
                            print(block.text[:200])
                        else:
                            print(f"<{type(block).__name__}>")
            elif hasattr(message, "usage"):
                print(f"Tokens: {message.usage.input_tokens} in, {message.usage.output_tokens} out")
            else:
                print()

    asyncio.run(main())

except ImportError:
    print("Claude Agent SDK not installed.")
    print("Install with: pip install claude-agent-sdk")
    print()
    print("This example demonstrates the instrumentation API:")
    print()
    print("  from traceai_claude_agent_sdk import ClaudeAgentInstrumentor")
    print("  ClaudeAgentInstrumentor().instrument()")
    print()
    print("Once instrumented, all Claude Agent SDK operations will be traced.")
