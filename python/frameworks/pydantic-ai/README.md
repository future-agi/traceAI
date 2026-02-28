# TraceAI Pydantic AI Instrumentation

OpenTelemetry instrumentation for [Pydantic AI](https://ai.pydantic.dev), providing comprehensive tracing for AI agent executions, tool calls, and model interactions.

## Installation

```bash
pip install traceai-pydantic-ai
```

## Quick Start

```python
from traceai_pydantic_ai import PydanticAIInstrumentor

# Initialize instrumentation (do this once at startup)
PydanticAIInstrumentor().instrument()

# Use Pydantic AI as normal - tracing is automatic
from pydantic_ai import Agent

agent = Agent('openai:gpt-4o', instructions='Be concise and helpful.')
result = agent.run_sync('What is the capital of France?')
print(result.output)
```

## Features

- **Agent Run Tracing**: Automatic tracing for `run()`, `run_sync()`, and `run_stream()` methods
- **Tool Call Tracking**: Traces tool function executions with inputs and outputs
- **Model Attribution**: Automatic detection of model provider (OpenAI, Anthropic, Google, etc.)
- **Usage Metrics**: Token usage tracking (input, output, total)
- **Streaming Support**: Full tracing for streaming responses with chunk counting
- **Error Handling**: Comprehensive error recording and exception tracking
- **Structured Outputs**: Support for Pydantic model result types
- **Conversation Memory**: Message history length tracking
- **Multi-model Support**: Traces across different model providers

## Configuration

### Basic Setup

```python
from traceai_pydantic_ai import PydanticAIInstrumentor

# Use default tracer provider
PydanticAIInstrumentor().instrument()
```

### With Custom Tracer Provider

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from traceai_pydantic_ai import PydanticAIInstrumentor

# Setup tracer provider
provider = TracerProvider()
processor = BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4317"))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

# Instrument with provider
PydanticAIInstrumentor().instrument(tracer_provider=provider)
```

### Convenience Function

```python
from traceai_pydantic_ai import instrument_pydantic_ai

# One-liner instrumentation
instrumentor = instrument_pydantic_ai()
```

### Using Built-in Instrumentation

Pydantic AI has its own OpenTelemetry support. You can optionally use that instead:

```python
from traceai_pydantic_ai import instrument_pydantic_ai

# Use Pydantic AI's built-in OTEL (via Agent.instrument_all())
instrument_pydantic_ai(use_builtin=True)
```

## Examples

### Basic Agent

```python
from traceai_pydantic_ai import PydanticAIInstrumentor
from pydantic_ai import Agent

# Initialize instrumentation
PydanticAIInstrumentor().instrument()

# Create agent
agent = Agent(
    'openai:gpt-4o',
    instructions='You are a helpful assistant.'
)

# Synchronous execution
result = agent.run_sync('Hello, how are you?')
print(result.output)
```

### Async Agent

```python
import asyncio
from traceai_pydantic_ai import PydanticAIInstrumentor
from pydantic_ai import Agent

PydanticAIInstrumentor().instrument()

agent = Agent('anthropic:claude-3-sonnet')

async def main():
    result = await agent.run('Tell me a joke')
    print(result.output)

asyncio.run(main())
```

### Agent with Tools

```python
from traceai_pydantic_ai import PydanticAIInstrumentor
from pydantic_ai import Agent, RunContext

PydanticAIInstrumentor().instrument()

agent = Agent('openai:gpt-4o')

@agent.tool
def get_weather(ctx: RunContext, city: str) -> str:
    """Get the weather for a city."""
    return f"The weather in {city} is sunny and 72°F"

@agent.tool
def calculate(ctx: RunContext, expression: str) -> str:
    """Evaluate a mathematical expression."""
    return str(eval(expression))

result = agent.run_sync('What is the weather in San Francisco?')
print(result.output)
```

### Structured Outputs

```python
from pydantic import BaseModel
from traceai_pydantic_ai import PydanticAIInstrumentor
from pydantic_ai import Agent

PydanticAIInstrumentor().instrument()

class CityInfo(BaseModel):
    name: str
    country: str
    population: int

agent = Agent('openai:gpt-4o', result_type=CityInfo)
result = agent.run_sync('Tell me about Paris')

city = result.output
print(f"{city.name}, {city.country} - Population: {city.population:,}")
```

### Streaming

```python
import asyncio
from traceai_pydantic_ai import PydanticAIInstrumentor
from pydantic_ai import Agent

PydanticAIInstrumentor().instrument()

agent = Agent('openai:gpt-4o')

async def main():
    async with agent.run_stream('Write a haiku about Python') as stream:
        async for chunk in stream.stream_text():
            print(chunk, end='', flush=True)

asyncio.run(main())
```

### RAG Pipeline

```python
from typing import List
from pydantic import BaseModel, Field
from traceai_pydantic_ai import PydanticAIInstrumentor
from pydantic_ai import Agent, RunContext

PydanticAIInstrumentor().instrument()

class RAGResponse(BaseModel):
    answer: str = Field(description="The answer to the question")
    sources: List[str] = Field(description="Source documents used")
    confidence: float = Field(description="Confidence score 0-1")

agent = Agent('openai:gpt-4o', result_type=RAGResponse)

@agent.tool
def search_documents(ctx: RunContext, query: str) -> str:
    """Search the knowledge base."""
    # In production: call vector database
    return "Found relevant documents..."

result = agent.run_sync("What are Python best practices?")
print(f"Answer: {result.output.answer}")
print(f"Sources: {result.output.sources}")
```

### Conversation with Memory

```python
import asyncio
from traceai_pydantic_ai import PydanticAIInstrumentor
from pydantic_ai import Agent

PydanticAIInstrumentor().instrument()

agent = Agent('openai:gpt-4o', instructions='Remember details from our conversation.')

async def main():
    history = []

    # First turn
    result = await agent.run("My name is Alice.")
    history.append({"role": "user", "content": "My name is Alice."})
    history.append({"role": "assistant", "content": str(result.output)})

    # Second turn with history
    result = await agent.run(
        "What's my name?",
        message_history=history
    )
    print(result.output)  # Should remember "Alice"

asyncio.run(main())
```

### Multi-Model Routing

```python
from traceai_pydantic_ai import PydanticAIInstrumentor, get_model_provider
from pydantic_ai import Agent

PydanticAIInstrumentor().instrument()

# Route to different models based on task
def get_agent_for_task(task_type: str) -> Agent:
    if task_type == "coding":
        return Agent("anthropic:claude-3-sonnet")
    elif task_type == "analysis":
        return Agent("openai:gpt-4o")
    else:
        return Agent("openai:gpt-4o-mini")  # Cost-effective default

agent = get_agent_for_task("coding")
result = agent.run_sync("Write a Python function to calculate fibonacci")
print(f"Provider: {get_model_provider('anthropic:claude-3-sonnet')}")
print(result.output)
```

### Error Handling

```python
from traceai_pydantic_ai import PydanticAIInstrumentor
from pydantic_ai import Agent, RunContext

PydanticAIInstrumentor().instrument()

agent = Agent('openai:gpt-4o')

@agent.tool
def risky_operation(ctx: RunContext, value: int) -> int:
    """Perform a risky operation."""
    if value == 0:
        raise ValueError("Cannot process zero!")
    return value * 2

# Errors are captured in spans with full context
try:
    result = agent.run_sync("Process the value 0")
except Exception as e:
    print(f"Error captured in trace: {e}")
```

## Traced Attributes

### Agent Run Spans

| Attribute | Description |
|-----------|-------------|
| `pydantic_ai.span_kind` | Type of span (agent_run, tool_call, stream) |
| `pydantic_ai.run.id` | Unique identifier for the run |
| `pydantic_ai.run.method` | Method used (run, run_sync, run_stream) |
| `pydantic_ai.run.prompt` | Input prompt |
| `pydantic_ai.run.result` | Output result |
| `pydantic_ai.run.is_structured` | Whether output is structured |
| `pydantic_ai.run.message_history_length` | Number of messages in history |
| `pydantic_ai.agent.name` | Agent name (if set) |
| `pydantic_ai.agent.instructions` | Agent instructions |
| `pydantic_ai.agent.result_type` | Expected result type |
| `gen_ai.request.model` | Model name |
| `gen_ai.system` | Model provider |

### Usage Metrics

| Attribute | Description |
|-----------|-------------|
| `gen_ai.usage.input_tokens` | Input/prompt tokens |
| `gen_ai.usage.output_tokens` | Output/completion tokens |
| `gen_ai.usage.total_tokens` | Total tokens used |

### Tool Call Spans

| Attribute | Description |
|-----------|-------------|
| `pydantic_ai.tool.name` | Tool function name |
| `pydantic_ai.tool.description` | Tool description |
| `pydantic_ai.tool.args` | Tool arguments (JSON) |
| `pydantic_ai.tool.result` | Tool return value |
| `pydantic_ai.tool.is_error` | Whether tool raised error |
| `pydantic_ai.tool.error_message` | Error message if failed |
| `pydantic_ai.tool.duration_ms` | Tool execution time |

### Streaming Spans

| Attribute | Description |
|-----------|-------------|
| `pydantic_ai.stream.chunk_count` | Number of chunks received |
| `pydantic_ai.stream.is_structured` | Whether streaming structured data |

### Error Attributes

| Attribute | Description |
|-----------|-------------|
| `pydantic_ai.is_error` | Whether an error occurred |
| `pydantic_ai.error.type` | Exception type |
| `pydantic_ai.error.message` | Error message |

### Performance Attributes

| Attribute | Description |
|-----------|-------------|
| `pydantic_ai.duration_ms` | Total execution time |
| `pydantic_ai.time_to_first_token_ms` | Time to first token (streaming) |

## Model Provider Detection

The instrumentor automatically detects model providers from model names:

| Pattern | Provider |
|---------|----------|
| `gpt-*`, `o1-*`, `o3-*` | openai |
| `claude-*` | anthropic |
| `gemini*` | google |
| `mistral*` | mistral |
| `deepseek*` | deepseek |
| `groq*` | groq |
| `cohere*` | cohere |
| `ollama*` | ollama |
| `together*` | together |
| `openai:*` | openai |
| `anthropic:*` | anthropic |
| `bedrock:*` | aws |
| `vertex:*` | google |

## Uninstrumenting

```python
from traceai_pydantic_ai import PydanticAIInstrumentor

instrumentor = PydanticAIInstrumentor()
instrumentor.instrument()

# ... use Pydantic AI ...

# Remove instrumentation
instrumentor.uninstrument()
```

## Integration with FutureAGI

For the best experience, use with FutureAGI's observability platform:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pydantic_ai import PydanticAIInstrumentor

# Register with FutureAGI
trace_provider = register(
    api_key="your-api-key",
    project_type=ProjectType.OBSERVE,
    project_name="my-pydantic-ai-app",
)

# Instrument Pydantic AI
PydanticAIInstrumentor().instrument(tracer_provider=trace_provider)
```

## Running Tests

```bash
# Run unit tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run integration tests (requires pydantic-ai and API key)
OPENAI_API_KEY=your-key pytest tests/ -m integration
```

## Example Scripts

The `examples/` directory contains ready-to-run scripts:

| Script | Description |
|--------|-------------|
| `basic_agent.py` | Simple agent usage |
| `agent_with_tools.py` | Tool/function calling |
| `structured_output.py` | Pydantic model outputs |
| `async_streaming.py` | Streaming responses |
| `multi_model.py` | Multiple model providers |
| `rag_pipeline.py` | RAG with retrieval tools |
| `conversation_memory.py` | Multi-turn conversations |
| `error_handling.py` | Error capture patterns |
| `futureagi_integration.py` | FutureAGI platform setup |

## Requirements

- Python >= 3.9
- pydantic-ai >= 0.0.1
- opentelemetry-api >= 1.0.0
- opentelemetry-sdk >= 1.0.0

## License

MIT License - see LICENSE file for details.
