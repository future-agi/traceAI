# TraceAI BeeAI Framework Integration

Comprehensive observability for [IBM BeeAI Framework](https://framework.beeai.dev/) with TraceAI.

BeeAI Framework is an open-source toolkit from IBM Research for building production-grade multi-agent systems. This integration provides seamless tracing by leveraging BeeAI's built-in OpenInference instrumentation.

## Features

- **Zero-config integration**: Works with BeeAI's native OpenInference support
- **Automatic tracing**: Agent runs, tool calls, LLM interactions, and workflows
- **Token tracking**: Input/output tokens and usage metrics
- **Session correlation**: Link traces across conversations
- **Custom middleware**: Extended event capture for detailed observability
- **IBM Granite support**: Optimized for IBM Granite and Llama models

## Installation

```bash
pip install traceai-beeai
```

For full functionality with BeeAI:

```bash
pip install beeai-framework openinference-instrumentation-beeai
```

## Quick Start

**IMPORTANT**: Call `configure_beeai_tracing()` BEFORE importing BeeAI modules!

### Option 1: Using TraceAI with fi_instrumentation

```python
# Setup TraceAI FIRST
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_beeai import configure_beeai_tracing

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-beeai-agent",
)

configure_beeai_tracing(tracer_provider=trace_provider)

# NOW import BeeAI modules
from beeai_framework.agents import Agent
from beeai_framework.backend.chat import ChatModel

model = ChatModel.from_name("ollama:granite3.1-dense:8b")
agent = Agent(
    llm=model,
    role="Assistant",
    instructions="You are a helpful assistant.",
)

response = agent.run("Hello!")
```

### Option 2: Direct OTLP Configuration

```python
from traceai_beeai import configure_beeai_tracing

# Configure with OTLP endpoint directly
configure_beeai_tracing(
    otlp_endpoint="https://api.traceai.com/v1/traces",
    otlp_headers={"Authorization": "Bearer YOUR_API_KEY"},
    project_name="my-beeai-agent",
)

# Now import and use BeeAI
from beeai_framework.agents import Agent
# ...
```

## Using Tools

BeeAI tools are automatically traced:

```python
from beeai_framework.agents import Agent
from beeai_framework.backend.chat import ChatModel
from beeai_framework.tools import WikipediaTool, OpenMeteoTool, ThinkTool

model = ChatModel.from_name("ollama:granite3.1-dense:8b")

agent = Agent(
    llm=model,
    role="Research Assistant",
    instructions="Use tools to find information.",
    tools=[
        ThinkTool(),       # Internal reasoning
        WikipediaTool(),   # Knowledge retrieval
        OpenMeteoTool(),   # Weather forecasts
    ],
)

response = agent.run("What's the weather in Paris?")
```

## Custom Middleware

For extended tracing with session tracking:

```python
from traceai_beeai import create_tracing_middleware

middleware = create_tracing_middleware(
    tracer_provider=trace_provider,
    capture_input=True,
    capture_output=True,
    session_id="user-session-123",
    user_id="user@example.com",
)

agent = Agent(
    llm=model,
    role="Assistant",
    instructions="You are helpful.",
    middlewares=[middleware],
)
```

## Using Requirements

BeeAI agents with requirements are fully traced:

```python
from beeai_framework.agents import Agent
from beeai_framework.requirements import ConditionalRequirement
from beeai_framework.tools import ThinkTool

agent = Agent(
    llm=model,
    role="Safe Assistant",
    instructions="Always think before responding.",
    tools=[ThinkTool()],
    requirements=[
        ConditionalRequirement(
            step=0,
            tool=ThinkTool,  # Force ThinkTool on first step
        ),
    ],
)
```

## Semantic Attributes

The integration captures these OpenTelemetry GenAI semantic attributes:

### Agent Attributes
| Attribute | Description |
|-----------|-------------|
| `agent.name` | Agent name |
| `agent.type` | Agent class name |
| `agent.role` | Agent role description |
| `agent.instructions` | Agent instructions (truncated) |
| `beeai.tool_count` | Number of tools available |
| `beeai.requirements` | Configured requirements |
| `beeai.memory.type` | Memory strategy type |

### Model Attributes
| Attribute | Description |
|-----------|-------------|
| `gen_ai.system` | Model provider (ibm, openai, etc.) |
| `gen_ai.request.model` | Model name/ID |
| `gen_ai.request.temperature` | Temperature setting |
| `gen_ai.request.max_tokens` | Max tokens setting |
| `gen_ai.usage.input_tokens` | Input token count |
| `gen_ai.usage.output_tokens` | Output token count |

### Tool Attributes
| Attribute | Description |
|-----------|-------------|
| `gen_ai.tool.name` | Tool name |
| `gen_ai.tool.description` | Tool description |
| `gen_ai.tool.parameters` | Tool input parameters |
| `gen_ai.tool.result` | Tool execution result |

### Workflow Attributes
| Attribute | Description |
|-----------|-------------|
| `beeai.workflow.name` | Workflow name |
| `beeai.workflow.step` | Current step name |
| `beeai.session.id` | Session identifier |
| `beeai.user.id` | User identifier |

## Model Provider Support

The integration automatically detects model providers:

| Provider | Model Patterns |
|----------|---------------|
| IBM | `granite-*`, `ibm/*` |
| OpenAI | `gpt-*`, `o1-*` |
| Anthropic | `claude-*` |
| Meta | `llama-*` |
| Google | `gemini-*` |
| Mistral | `mistral-*`, `mixtral-*` |
| Ollama | `ollama/*` |
| Groq | `groq/*` |
| Together | `together/*` |

## Environment Variables

BeeAI respects standard OpenTelemetry environment variables:

```bash
# OTLP endpoint
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://api.traceai.com/v1/traces"

# OTLP headers (authentication)
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer YOUR_API_KEY"
```

## Examples

See the `examples/` directory for complete examples:

- `basic_agent.py` - Simple agent with tracing
- `agent_with_tools.py` - Tools and function calling
- `requirements_agent.py` - Behavioral requirements
- `middleware_tracing.py` - Custom middleware with session tracking

## How It Works

BeeAI has native observability support via OpenInference instrumentation. This integration:

1. **Configures OpenTelemetry**: Sets up tracer provider and OTLP export
2. **Initializes BeeAIInstrumentor**: Wraps BeeAI internals for automatic span creation
3. **Provides middleware**: Optional `TraceAIMiddleware` for extended capture
4. **Adds helper functions**: Attribute extraction and trace context management

## Compatibility

- **BeeAI Framework**: >= 0.1.0
- **Python**: >= 3.11
- **OpenTelemetry**: >= 1.0.0
- **OpenInference**: >= 0.1.0

## Resources

- [BeeAI Framework Documentation](https://framework.beeai.dev/)
- [BeeAI GitHub](https://github.com/i-am-bee/beeai-framework)
- [IBM Research - BeeAI](https://research.ibm.com/projects/bee-ai-framework)
- [OpenTelemetry GenAI Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [TraceAI Documentation](https://docs.futureagi.com/traceai)

## License

Apache-2.0
