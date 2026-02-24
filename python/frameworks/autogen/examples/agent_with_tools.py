"""AutoGen v0.4 agent with tools example.

This example demonstrates:
- Defining tool functions for the agent
- Agent using tools to accomplish tasks
- Tracing tool calls with inputs and outputs
"""

import asyncio
import os
import json
from datetime import datetime
from typing import Annotated

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import instrument_autogen

# Setup tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-agent-tools",
)
instrument_autogen(tracer_provider=trace_provider)

# Import AutoGen v0.4
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_ext.models.openai import OpenAIChatCompletionClient


# Define tools for the agent
def get_weather(
    city: Annotated[str, "The city name to get weather for"],
) -> str:
    """Get the current weather for a city."""
    # Simulated weather data
    weather_data = {
        "new york": {"temp": 72, "condition": "sunny", "humidity": 45},
        "london": {"temp": 58, "condition": "cloudy", "humidity": 78},
        "tokyo": {"temp": 68, "condition": "partly cloudy", "humidity": 60},
        "paris": {"temp": 65, "condition": "rainy", "humidity": 82},
    }

    city_lower = city.lower()
    if city_lower in weather_data:
        w = weather_data[city_lower]
        return f"Weather in {city}: {w['temp']}°F, {w['condition']}, {w['humidity']}% humidity"
    else:
        return f"Weather data not available for {city}"


def calculate(
    expression: Annotated[str, "Mathematical expression to evaluate"],
) -> str:
    """Evaluate a mathematical expression safely."""
    try:
        # Safe evaluation of math expressions
        allowed_chars = set("0123456789+-*/.(). ")
        if not all(c in allowed_chars for c in expression):
            return "Error: Invalid characters in expression"

        result = eval(expression)
        return f"Result: {expression} = {result}"
    except Exception as e:
        return f"Error evaluating expression: {str(e)}"


def get_current_time(
    timezone: Annotated[str, "Timezone name (e.g., 'UTC', 'EST', 'PST')"] = "UTC",
) -> str:
    """Get the current time in a specified timezone."""
    now = datetime.now()
    # Simplified timezone handling
    tz_offsets = {"UTC": 0, "EST": -5, "PST": -8, "CET": 1, "JST": 9}
    offset = tz_offsets.get(timezone.upper(), 0)

    return f"Current time ({timezone}): {now.strftime('%Y-%m-%d %H:%M:%S')} (offset: {offset}h)"


def search_database(
    query: Annotated[str, "Search query"],
    limit: Annotated[int, "Maximum number of results"] = 5,
) -> str:
    """Search a simulated database for information."""
    # Simulated database results
    results = [
        {"id": 1, "title": "Python Programming", "relevance": 0.95},
        {"id": 2, "title": "Machine Learning Basics", "relevance": 0.87},
        {"id": 3, "title": "Data Science Guide", "relevance": 0.82},
    ]

    return json.dumps(
        {
            "query": query,
            "results": results[:limit],
            "total_found": len(results),
        },
        indent=2,
    )


async def main():
    # Create model client
    model = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    # Create agent with tools
    agent = AssistantAgent(
        name="assistant",
        model_client=model,
        tools=[get_weather, calculate, get_current_time, search_database],
        system_message="""You are a helpful assistant with access to tools.
        Use the available tools to help answer questions.
        Always explain what you're doing and why.""",
    )

    # Test queries that will trigger tool use
    queries = [
        "What's the weather like in Tokyo?",
        "Calculate 15 * 7 + 23",
        "What time is it in EST?",
        "Search for information about Python",
    ]

    for query in queries:
        print(f"\n{'=' * 50}")
        print(f"Query: {query}")
        print("-" * 50)

        response = await agent.on_messages(
            messages=[TextMessage(content=query, source="user")],
            cancellation_token=None,
        )

        print(f"Response: {response.chat_message.content}")

        # Show tool calls if any
        if hasattr(response, "inner_messages") and response.inner_messages:
            for msg in response.inner_messages:
                msg_type = type(msg).__name__
                if "ToolCall" in msg_type:
                    print(f"  [Tool called: {getattr(msg, 'name', 'unknown')}]")


if __name__ == "__main__":
    asyncio.run(main())
