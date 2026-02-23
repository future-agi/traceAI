# TraceAI Agno Instrumentation

OpenTelemetry instrumentation for [Agno](https://github.com/agno-agi/agno), the high-performance AI agent framework.

## Installation

```bash
pip install traceai-agno
```

For full functionality, also install the Agno framework and its OpenInference instrumentation:

```bash
pip install agno openinference-instrumentation-agno
```

## Quick Start

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_agno import configure_agno_tracing

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="agno-agent",
)

# Configure Agno to use TraceAI (call BEFORE creating agents)
configure_agno_tracing(tracer_provider=trace_provider)

# Now import and use Agno normally
from agno.agent import Agent
from agno.models.openai import OpenAIChat

agent = Agent(
    model=OpenAIChat(id="gpt-4"),
    description="A helpful assistant",
)

response = agent.run("What is the capital of France?")
print(response.content)
```

## Configuration Options

### Using fi_instrumentation (Recommended)

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_agno import configure_agno_tracing

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-agno-project",
)

configure_agno_tracing(tracer_provider=trace_provider)
```

### Direct OTLP Configuration

```python
from traceai_agno import configure_agno_tracing

configure_agno_tracing(
    otlp_endpoint="https://api.traceai.com/v1/traces",
    otlp_headers={"Authorization": "Bearer YOUR_API_KEY"},
    project_name="my-agno-project",
)
```

### Using Custom Tracer Provider

```python
from traceai_agno import setup_traceai_exporter, configure_agno_tracing

# Create a custom tracer provider
provider = setup_traceai_exporter(
    endpoint="https://api.traceai.com/v1/traces",
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    service_name="my-agno-service",
    use_batch_processor=True,
)

configure_agno_tracing(tracer_provider=provider)
```

## Features

### Agent Tracing

Automatically captures:
- Agent name, type, and configuration
- Model information (provider, ID, temperature, etc.)
- Tool count and configuration
- Memory and knowledge settings
- Debug mode and markdown settings

### Tool Tracing

Tracks tool executions including:
- Tool name and description
- Input parameters
- Execution results

### Team Tracing

For multi-agent setups:
- Team name
- Team member agents
- Inter-agent communication

### Workflow Tracing

For complex workflows:
- Workflow name
- Step execution
- State transitions

## Span Attributes

The instrumentation adds the following attributes to spans:

### LLM Attributes (OpenTelemetry GenAI Semantic Conventions)
- `gen_ai.system` - Model provider (openai, anthropic, etc.)
- `gen_ai.request.model` - Requested model ID
- `gen_ai.request.temperature` - Temperature setting
- `gen_ai.request.max_tokens` - Max tokens setting
- `gen_ai.usage.input_tokens` - Input token count
- `gen_ai.usage.output_tokens` - Output token count

### Agent Attributes
- `agent.name` - Agent name
- `agent.type` - Agent type
- `agent.description` - Agent description
- `agent.instructions` - Agent instructions

### Agno-Specific Attributes
- `agno.agent.id` - Agent ID
- `agno.tool_count` - Number of tools
- `agno.team.name` - Team name
- `agno.team.members` - Team member names
- `agno.workflow.name` - Workflow name
- `agno.session.id` - Session ID
- `agno.user.id` - User ID
- `agno.debug_mode` - Debug mode status
- `agno.memory.enabled` - Memory enabled status
- `agno.knowledge.enabled` - Knowledge enabled status

## Helper Functions

### Extract Agent Attributes

```python
from traceai_agno import get_agent_attributes

agent = Agent(model=OpenAIChat(id="gpt-4"), name="MyAgent")
attrs = get_agent_attributes(agent)
# {'agent.name': 'MyAgent', 'agent.type': 'Agent', ...}
```

### Extract Tool Attributes

```python
from traceai_agno import get_tool_attributes

def my_tool(query: str) -> str:
    """Search for information."""
    return "result"

attrs = get_tool_attributes(my_tool)
# {'gen_ai.tool.name': 'my_tool', 'gen_ai.tool.description': 'Search for information.'}
```

### Extract Team Attributes

```python
from traceai_agno import get_team_attributes

team = Team(name="ResearchTeam", agents=[agent1, agent2])
attrs = get_team_attributes(team)
# {'agno.team.name': 'ResearchTeam', 'agno.team.members': 'Agent1, Agent2'}
```

### Detect Model Provider

```python
from traceai_agno import get_model_provider

provider = get_model_provider("gpt-4")  # Returns "openai"
provider = get_model_provider("claude-3-sonnet")  # Returns "anthropic"
provider = get_model_provider("ollama/llama3")  # Returns "ollama"
```

### Create Trace Context

```python
from traceai_agno import create_trace_context

context = create_trace_context(
    session_id="session-123",
    user_id="user-456",
    tags=["production"],
    metadata={"environment": "prod"},
)
```

## Examples

See the [examples](./examples) directory for complete usage examples:

- `basic_agent.py` - Simple agent with tracing
- `agent_with_tools.py` - Agent with tool calling
- `team_example.py` - Multi-agent team coordination
- `workflow_example.py` - Complex workflow tracing

## Requirements

- Python >= 3.10
- opentelemetry-api >= 1.0.0
- opentelemetry-sdk >= 1.0.0
- opentelemetry-exporter-otlp >= 1.0.0
- fi-instrumentation >= 0.1.0

## License

Apache-2.0
