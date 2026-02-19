"""Basic AutoGen v0.2 agent example with tracing.

This example demonstrates:
- Setting up instrumentation for AutoGen v0.2
- Creating AssistantAgent and UserProxyAgent
- Running a simple conversation with automatic tracing
"""

import os

# Setup tracing first
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import AutogenInstrumentor

# Register with FutureAGI (or use any OTEL backend)
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-basic-example",
)

# Instrument AutoGen
AutogenInstrumentor().instrument(tracer_provider=trace_provider)

# Now import and use AutoGen
import autogen

# Configure LLM
llm_config = {
    "config_list": [
        {
            "model": "gpt-4",
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
    ],
    "temperature": 0,
}

# Create assistant agent
assistant = autogen.AssistantAgent(
    name="assistant",
    llm_config=llm_config,
    system_message="""You are a helpful AI assistant.
    Provide clear and concise answers.
    When asked to code, provide working Python code.""",
)

# Create user proxy agent (simulates human)
user_proxy = autogen.UserProxyAgent(
    name="user_proxy",
    human_input_mode="NEVER",  # No human input required
    max_consecutive_auto_reply=3,
    code_execution_config={"work_dir": "coding", "use_docker": False},
)

# Run the conversation - automatically traced
if __name__ == "__main__":
    print("Starting conversation...")

    result = user_proxy.initiate_chat(
        assistant,
        message="Write a Python function to check if a string is a palindrome.",
    )

    print("\n--- Conversation Complete ---")
    print(f"Messages exchanged: {len(result.chat_history)}")

    # Access the last message
    if result.chat_history:
        print(f"\nLast message: {result.chat_history[-1]['content'][:200]}...")
