"""Minimal Microsoft Agent Framework example traced into Future AGI.

Calls a real LLM (OpenAI) and a real tool (wttr.in — no API key needed).
You should see invoke_agent / chat / execute_tool spans in the FI dashboard.

Run with:
    export FI_API_KEY=...
    export FI_SECRET_KEY=...
    export OPENAI_API_KEY=...
    python examples/basic_agent.py
"""

import asyncio
import os
import urllib.parse
import urllib.request

from agent_framework import Agent
from agent_framework.observability import enable_instrumentation
from agent_framework.openai import OpenAIChatClient
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

from traceai_agent_framework import enable_fi_attribute_mapping


def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: City name, e.g. "Paris", "Tokyo", "New York".
    """
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read().decode("utf-8").strip()


def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY before running this example.")

    # 1) FI tracer provider on the global slot so agent_framework reads from there.
    register(
        project_type=ProjectType.OBSERVE,
        project_name="agent-framework-basic-example",
        set_global_tracer_provider=True,
    )

    # 2) Agent Framework's own observability + sensitive-data opt-in so messages
    #    are captured in spans (required to see prompts/completions in FI).
    enable_instrumentation(enable_sensitive_data=True)

    # 3) Install our SpanProcessor on the FI tracer provider; it re-keys
    #    Agent Framework's gen_ai.* spans into FI conventions as they end.
    enable_fi_attribute_mapping()

    # 4) Build an agent with a real weather tool.
    agent = Agent(
        OpenAIChatClient(model="gpt-4o-mini"),
        name="weather_agent",
        description="Answers questions about weather using the get_weather tool.",
        instructions="You are a concise weather assistant. Always use get_weather.",
        tools=[get_weather],
    )

    async def run() -> None:
        response = await agent.run("What's the weather in Paris right now?")
        print("Agent response:\n", response)

    asyncio.run(run())


if __name__ == "__main__":
    main()
