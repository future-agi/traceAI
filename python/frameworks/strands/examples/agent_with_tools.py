"""Strands Agent with tools example with TraceAI observability.

This example demonstrates:
- Creating agents with custom tools
- Using the @tool decorator
- Tracing tool execution
"""

import os
from typing import Annotated

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_strands import configure_strands_tracing

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="strands-tools-agent",
)

# Configure Strands to use TraceAI
configure_strands_tracing(tracer_provider=trace_provider)

# Import Strands (after configuration)
from strands import Agent, tool


# Define custom tools using the @tool decorator
@tool
def calculate(
    operation: Annotated[str, "The operation: add, subtract, multiply, divide"],
    a: Annotated[float, "First number"],
    b: Annotated[float, "Second number"],
) -> str:
    """Perform a mathematical calculation."""
    operations = {
        "add": lambda x, y: x + y,
        "subtract": lambda x, y: x - y,
        "multiply": lambda x, y: x * y,
        "divide": lambda x, y: x / y if y != 0 else "Error: Division by zero",
    }

    if operation not in operations:
        return f"Error: Unknown operation '{operation}'"

    result = operations[operation](a, b)
    return f"{a} {operation} {b} = {result}"


@tool
def get_weather(
    city: Annotated[str, "The city to get weather for"],
) -> str:
    """Get the current weather for a city (simulated)."""
    # Simulated weather data
    weather_data = {
        "new york": {"temp": 72, "condition": "Sunny"},
        "london": {"temp": 58, "condition": "Cloudy"},
        "tokyo": {"temp": 68, "condition": "Partly Cloudy"},
        "paris": {"temp": 65, "condition": "Rainy"},
    }

    city_lower = city.lower()
    if city_lower in weather_data:
        data = weather_data[city_lower]
        return f"Weather in {city}: {data['temp']}°F, {data['condition']}"
    else:
        return f"Weather data not available for {city}"


@tool
def search_database(
    query: Annotated[str, "The search query"],
) -> str:
    """Search a database for information (simulated)."""
    # Simulated database
    database = {
        "python": "Python is a high-level programming language known for its simplicity.",
        "javascript": "JavaScript is a scripting language used for web development.",
        "aws": "Amazon Web Services (AWS) is a cloud computing platform.",
        "ai": "Artificial Intelligence (AI) refers to machine intelligence.",
    }

    query_lower = query.lower()
    for key, value in database.items():
        if key in query_lower:
            return value

    return f"No results found for: {query}"


def main():
    # Create an agent with tools
    agent = Agent(
        model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        system_prompt="""You are a helpful assistant with access to tools.
        Use the appropriate tool when needed to answer questions.
        Always explain what you're doing.""",
        tools=[calculate, get_weather, search_database],
        trace_attributes={
            "session.id": "tools-demo-001",
            "tags": ["demo", "tools"],
        },
    )

    # Test queries that require different tools
    queries = [
        "What is 15 multiplied by 7?",
        "What's the weather like in Tokyo?",
        "Tell me about Python programming.",
        "Can you calculate 100 divided by 4 and then tell me the weather in London?",
    ]

    print("=" * 60)
    print("Strands Agent with Tools Demo")
    print("=" * 60)
    print(f"\nAvailable tools: calculate, get_weather, search_database")

    for query in queries:
        print(f"\n{'-' * 60}")
        print(f"User: {query}")
        response = agent(query)
        print(f"Agent: {response}")

    print("\n" + "=" * 60)
    print("All tool executions have been traced!")
    print("View the traces in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
