"""Agno agent with tools example with TraceAI instrumentation.

This example demonstrates how to set up tracing for an agent with tool calling.
"""

import os
from typing import Optional

# Setup tracing BEFORE importing Agno
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_agno import configure_agno_tracing

# Initialize TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="agno-tools-example",
)

# Configure Agno to use TraceAI
configure_agno_tracing(tracer_provider=trace_provider)

# Now import Agno modules
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools import tool


# Define tools
@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: The name of the city to get weather for.

    Returns:
        A string describing the current weather.
    """
    # Mock weather data
    weather_data = {
        "new york": "Sunny, 72°F (22°C)",
        "london": "Cloudy, 59°F (15°C)",
        "tokyo": "Rainy, 68°F (20°C)",
        "paris": "Partly cloudy, 65°F (18°C)",
        "sydney": "Clear, 77°F (25°C)",
    }
    city_lower = city.lower()
    return weather_data.get(city_lower, f"Weather data not available for {city}")


@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression.

    Args:
        expression: A mathematical expression to evaluate (e.g., "2 + 2", "10 * 5").

    Returns:
        The result of the calculation.
    """
    try:
        # Only allow safe mathematical operations
        allowed_chars = set("0123456789+-*/.(). ")
        if not all(c in allowed_chars for c in expression):
            return "Error: Invalid characters in expression"
        result = eval(expression)
        return str(result)
    except Exception as e:
        return f"Error: {str(e)}"


@tool
def search_knowledge(query: str, topic: Optional[str] = None) -> str:
    """Search for information about a topic.

    Args:
        query: The search query.
        topic: Optional topic to narrow down the search.

    Returns:
        Relevant information about the query.
    """
    # Mock knowledge base
    knowledge = {
        "python": "Python is a high-level programming language known for readability.",
        "machine learning": "Machine learning is a subset of AI focused on learning from data.",
        "agno": "Agno is a high-performance AI agent framework for building intelligent agents.",
    }

    query_lower = query.lower()
    for key, value in knowledge.items():
        if key in query_lower:
            return value
    return f"No specific information found for: {query}"


def main():
    """Run an agent with tools example."""
    # Create an agent with tools
    agent = Agent(
        name="ToolAgent",
        model=OpenAIChat(id="gpt-4"),
        description="An assistant that can check weather, perform calculations, and search for information.",
        tools=[get_weather, calculate, search_knowledge],
        instructions=[
            "Use the available tools to help answer questions",
            "For weather questions, use the get_weather tool",
            "For math problems, use the calculate tool",
            "For information queries, use the search_knowledge tool",
        ],
        show_tool_calls=True,
        markdown=True,
    )

    # Example queries that use different tools
    queries = [
        "What's the weather like in Paris?",
        "What is 25 * 4 + 10?",
        "Tell me about machine learning",
        "What's the weather in Tokyo and calculate 100 / 5",
    ]

    print("Agent: ToolAgent")
    print("Tools: get_weather, calculate, search_knowledge")
    print("=" * 50)

    for query in queries:
        print(f"\nQuery: {query}")
        print("-" * 40)
        response = agent.run(query)
        print(f"Response: {response.content}")


if __name__ == "__main__":
    main()
