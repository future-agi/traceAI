# TraceAI AutoGen Instrumentation

OpenTelemetry instrumentation for [Microsoft AutoGen](https://microsoft.github.io/autogen/), providing comprehensive tracing for multi-agent conversations, tool executions, and LLM interactions.

**Supports both AutoGen v0.2 (legacy) and v0.4 (AgentChat).**

## Installation

```bash
pip install traceAI-autogen
```

### AutoGen Versions

This package supports both major AutoGen versions:

**AutoGen v0.2 (legacy)**:
```bash
pip install autogen>=0.2.0
```

**AutoGen v0.4 (AgentChat)**:
```bash
pip install autogen-agentchat>=0.4.0
```

## Quick Start

### Set Environment Variables

```python
import os

os.environ["FI_API_KEY"] = "your-api-key"
os.environ["FI_SECRET_KEY"] = "your-secret-key"
os.environ["OPENAI_API_KEY"] = "your-openai-key"
```

### Register Tracer Provider

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen_app"
)
```

### Instrument AutoGen

```python
from traceai_autogen import AutogenInstrumentor

AutogenInstrumentor().instrument(tracer_provider=trace_provider)
```

Or use the convenience function:

```python
from traceai_autogen import instrument_autogen

instrumentor = instrument_autogen(tracer_provider=trace_provider)
```

## Examples

### AutoGen v0.2 (Legacy)

```python
import autogen
from traceai_autogen import instrument_autogen

# Instrument AutoGen
instrument_autogen()

# Configure LLM
llm_config = {
    "config_list": [{"model": "gpt-4", "api_key": os.environ["OPENAI_API_KEY"]}],
    "temperature": 0,
}

# Create agents
assistant = autogen.AssistantAgent(
    name="assistant",
    llm_config=llm_config,
    system_message="You are a helpful AI assistant."
)

user_proxy = autogen.UserProxyAgent(
    name="user_proxy",
    human_input_mode="NEVER",
    max_consecutive_auto_reply=3,
    code_execution_config={"work_dir": "coding", "use_docker": False},
)

# Start conversation - automatically traced
chat_result = user_proxy.initiate_chat(
    assistant,
    message="Write a Python function to calculate fibonacci numbers."
)
```

### AutoGen v0.4 (AgentChat)

```python
import asyncio
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_ext.models.openai import OpenAIChatCompletionClient
from traceai_autogen import instrument_autogen

# Instrument AutoGen
instrument_autogen()

async def main():
    # Create model client
    model_client = OpenAIChatCompletionClient(model="gpt-4o")

    # Create agents
    coder = AssistantAgent(
        name="coder",
        model_client=model_client,
        system_message="You are a Python expert. Write clean, efficient code.",
    )

    reviewer = AssistantAgent(
        name="reviewer",
        model_client=model_client,
        system_message="You review code and suggest improvements.",
    )

    # Create team
    team = RoundRobinGroupChat(
        participants=[coder, reviewer],
        termination_condition=MaxMessageTermination(max_messages=6),
    )

    # Run team task - automatically traced
    result = await team.run(task="Write a Python class for a binary search tree.")

    print(result.messages[-1].content)

asyncio.run(main())
```

### AutoGen v0.4 with Tools

```python
import asyncio
from autogen_agentchat.agents import AssistantAgent
from autogen_ext.models.openai import OpenAIChatCompletionClient
from traceai_autogen import instrument_autogen

instrument_autogen()

# Define tools
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"The weather in {city} is sunny and 72F."

def search_web(query: str) -> str:
    """Search the web for information."""
    return f"Search results for: {query}"

async def main():
    model_client = OpenAIChatCompletionClient(model="gpt-4o")

    agent = AssistantAgent(
        name="assistant",
        model_client=model_client,
        tools=[get_weather, search_web],
        system_message="You are a helpful assistant with access to tools.",
    )

    # Tool calls are automatically traced
    response = await agent.on_messages(
        [{"role": "user", "content": "What's the weather in San Francisco?"}],
        cancellation_token=None
    )

    print(response.chat_message.content)

asyncio.run(main())
```

### AutoGen v0.4 with Streaming

```python
import asyncio
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient
from traceai_autogen import instrument_autogen

instrument_autogen()

async def main():
    model_client = OpenAIChatCompletionClient(model="gpt-4o")

    agent = AssistantAgent(
        name="writer",
        model_client=model_client,
        system_message="You are a creative writer.",
    )

    team = RoundRobinGroupChat(participants=[agent])

    # Streaming is also traced
    async for message in team.run_stream(task="Write a haiku about coding"):
        print(message)

asyncio.run(main())
```

### Multi-Agent Code Review Team

```python
import asyncio
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_ext.models.openai import OpenAIChatCompletionClient
from traceai_autogen import instrument_autogen

instrument_autogen()

async def main():
    model = OpenAIChatCompletionClient(model="gpt-4o")

    # Create specialized agents
    architect = AssistantAgent(
        name="architect",
        model_client=model,
        system_message="You are a software architect. Design system architecture.",
    )

    developer = AssistantAgent(
        name="developer",
        model_client=model,
        system_message="You implement code based on architectural designs.",
    )

    tester = AssistantAgent(
        name="tester",
        model_client=model,
        system_message="You write tests and identify edge cases.",
    )

    # Selector-based team that picks the right agent
    team = SelectorGroupChat(
        participants=[architect, developer, tester],
        model_client=model,
        termination_condition=MaxMessageTermination(max_messages=10),
    )

    result = await team.run(
        task="Design and implement a REST API for a todo list application."
    )

    for msg in result.messages:
        print(f"{msg.source}: {msg.content[:100]}...")

asyncio.run(main())
```

## Features

### AutoGen v0.2 (Legacy) Features

- **Agent Conversations**: Traces `initiate_chat` calls between agents
- **Reply Generation**: Traces `generate_reply` for each agent response
- **Function Execution**: Traces tool/function calls via `execute_function`
- **Full Context**: Captures messages, responses, and metadata

### AutoGen v0.4 (AgentChat) Features

- **Agent Runs**: Traces `on_messages` for all agent types
- **Team Orchestration**: Traces `run` and `run_stream` for teams
- **Tool Execution**: Automatic tracing of tool function calls
- **Streaming Support**: Full tracing for streaming responses
- **Handoffs**: Traces agent handoffs in Swarm teams
- **Token Usage**: Captures token metrics from responses

## Traced Attributes

### Agent Spans

| Attribute | Description |
|-----------|-------------|
| `autogen.span_kind` | Type of span (agent_run, team_run, tool_call) |
| `autogen.agent.name` | Agent name |
| `autogen.agent.type` | Agent class name |
| `autogen.agent.tool_count` | Number of tools available |
| `autogen.agent.has_memory` | Whether agent has memory |
| `gen_ai.request.model` | Model name |

### Team Spans

| Attribute | Description |
|-----------|-------------|
| `autogen.team.type` | Team class name |
| `autogen.team.participant_count` | Number of participants |
| `autogen.team.participants` | JSON list of agent names |
| `autogen.team.max_turns` | Maximum turns configured |
| `autogen.team.termination_condition` | Termination condition type |

### Task/Run Spans

| Attribute | Description |
|-----------|-------------|
| `autogen.run.id` | Unique run identifier |
| `autogen.run.method` | Method name (run, run_stream) |
| `autogen.task.content` | Task/prompt content |
| `autogen.task.message_count` | Number of messages |
| `autogen.task.stop_reason` | Why the task stopped |

### Tool Spans

| Attribute | Description |
|-----------|-------------|
| `autogen.tool.name` | Tool function name |
| `autogen.tool.description` | Tool description |
| `autogen.tool.args` | Tool arguments (JSON) |
| `autogen.tool.result` | Tool return value |
| `autogen.tool.is_error` | Whether tool failed |
| `autogen.tool.duration_ms` | Execution time |

### Usage Metrics (GenAI Conventions)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.usage.input_tokens` | Input/prompt tokens |
| `gen_ai.usage.output_tokens` | Output/completion tokens |
| `gen_ai.usage.total_tokens` | Total tokens used |

### Error Attributes

| Attribute | Description |
|-----------|-------------|
| `autogen.is_error` | Whether an error occurred |
| `autogen.error.type` | Exception type |
| `autogen.error.message` | Error message |

## Model Provider Detection

The instrumentor automatically detects model providers:

| Pattern | Provider |
|---------|----------|
| `gpt-*`, `o1-*`, `o3-*` | openai |
| `claude-*` | anthropic |
| `gemini*` | google |
| `mistral*` | mistral |
| `deepseek*` | deepseek |
| `groq*` | groq |
| `ollama*` | ollama |

## Uninstrumenting

```python
from traceai_autogen import AutogenInstrumentor

instrumentor = AutogenInstrumentor()
instrumentor.instrument()

# ... use AutoGen ...

# Remove instrumentation
instrumentor.uninstrument()
```

## Integration with FutureAGI

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import AutogenInstrumentor

# Register with FutureAGI
trace_provider = register(
    api_key="your-api-key",
    project_type=ProjectType.OBSERVE,
    project_name="my-autogen-app",
)

# Instrument AutoGen
AutogenInstrumentor().instrument(tracer_provider=trace_provider)
```

## Running Tests

```bash
# Run all tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run specific test file
pytest tests/test_v04_wrapper.py -v
```

## Requirements

- Python >= 3.9
- opentelemetry-api >= 1.0.0
- opentelemetry-sdk >= 1.0.0
- fi-instrumentation-otel >= 0.1.11

**For v0.2**: autogen >= 0.2.0
**For v0.4**: autogen-agentchat >= 0.4.0

## License

MIT License - see LICENSE file for details.
