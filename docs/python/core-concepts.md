# Python Core Concepts

Understanding the core components of the traceAI Python SDK.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Your Application                     │
├─────────────────────────────────────────────────────────┤
│  Framework Instrumentor (OpenAIInstrumentor, etc.)      │
├─────────────────────────────────────────────────────────┤
│  fi_instrumentation (register, TraceConfig, etc.)       │
├─────────────────────────────────────────────────────────┤
│  OpenTelemetry SDK (TracerProvider, Spans, etc.)        │
├─────────────────────────────────────────────────────────┤
│  OTLP Exporter (HTTP or gRPC)                           │
└─────────────────────────────────────────────────────────┘
```

## The register() Function

The `register()` function is the entry point for setting up tracing:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_name="my_app",
    project_type=ProjectType.OBSERVE,
    # ... other options
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project_name` | str | env var | Project identifier |
| `project_type` | ProjectType | `EXPERIMENT` | OBSERVE or EXPERIMENT |
| `project_version_name` | str | None | Version (EXPERIMENT only) |
| `eval_tags` | list | None | Evaluation tags (EXPERIMENT only) |
| `metadata` | dict | None | Project metadata |
| `batch` | bool | `True` | Use batch span processor |
| `set_global_tracer_provider` | bool | `False` | Set as global provider |
| `headers` | dict | None | Custom auth headers |
| `verbose` | bool | `True` | Print config details |
| `transport` | Transport | `HTTP` | HTTP or GRPC |
| `semantic_convention` | SemanticConvention | `FI` | Attribute naming |

### Project Types

**OBSERVE** - For production monitoring:
```python
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="production_app",
    # Can use session_name
    # Cannot use eval_tags or project_version_name
)
```

**EXPERIMENT** - For development and testing:
```python
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="experiment",
    project_version_name="v1.0",
    eval_tags=[...],
    # Cannot use session_name
)
```

## Framework Instrumentors

Each framework has an instrumentor class that wraps SDK methods:

```python
from traceai_openai import OpenAIInstrumentor
from traceai_anthropic import AnthropicInstrumentor
from traceai_langchain import LangChainInstrumentor

# Instrument with your tracer provider
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
AnthropicInstrumentor().instrument(tracer_provider=trace_provider)
LangChainInstrumentor().instrument(tracer_provider=trace_provider)
```

### Instrumentor Methods

```python
instrumentor = OpenAIInstrumentor()

# Start instrumentation
instrumentor.instrument(tracer_provider=trace_provider)

# Stop instrumentation
instrumentor.uninstrument()

# Check if instrumented
is_active = instrumentor.is_instrumented()
```

## Span Types

traceAI captures different types of spans:

```python
from fi_instrumentation.fi_types import FiSpanKindValues

# Available span kinds
FiSpanKindValues.LLM        # Language model calls
FiSpanKindValues.AGENT      # Agent orchestration
FiSpanKindValues.TOOL       # Tool/function calls
FiSpanKindValues.CHAIN      # Workflow chains
FiSpanKindValues.RETRIEVER  # Document retrieval
FiSpanKindValues.EMBEDDING  # Embedding generation
FiSpanKindValues.RERANKER   # Result reranking
FiSpanKindValues.GUARDRAIL  # Safety checks
FiSpanKindValues.EVALUATOR  # Evaluation spans
FiSpanKindValues.VECTOR_DB  # Vector database ops
```

## TraceConfig

Controls data capture and privacy:

```python
from fi_instrumentation.instrumentation.config import TraceConfig

config = TraceConfig(
    hide_inputs=False,
    hide_outputs=False,
    hide_input_messages=False,
    hide_output_messages=False,
    hide_input_images=False,
    hide_input_text=False,
    hide_output_text=False,
    hide_embedding_vectors=False,
    hide_llm_invocation_parameters=False,
    base64_image_max_length=32000,
)
```

See [TraceConfig Reference](../configuration/trace-config.md) for details.

## FITracer

The `FITracer` wraps OpenTelemetry's Tracer with traceAI extensions:

```python
from fi_instrumentation.instrumentation._tracers import FITracer

tracer = FITracer(trace_provider.get_tracer(__name__))

# Create spans
with tracer.start_as_current_span("my_operation", fi_span_kind=FiSpanKindValues.CHAIN) as span:
    span.set_attribute("custom.attr", "value")
    # Your code here
```

### Decorators

```python
@tracer.chain(name="my_chain")
def my_chain_function():
    pass

@tracer.agent(name="my_agent")
def my_agent_function():
    pass

@tracer.tool(name="my_tool")
def my_tool_function():
    pass
```

## Span Attributes

traceAI captures these attributes automatically:

### Input/Output
- `input.value` - Raw input
- `output.value` - Raw output
- `input.mime_type` - Input content type
- `output.mime_type` - Output content type

### LLM-Specific
- `llm.model_name` - Model identifier
- `llm.provider` - Provider name (OpenAI, Anthropic, etc.)
- `llm.input_messages` - Input messages array
- `llm.output_messages` - Output messages array
- `llm.invocation_parameters` - Model parameters (temperature, etc.)
- `llm.tools` - Available tools/functions
- `llm.function_call` - Function call details

### Token Counts
- `llm.token_count.prompt` - Input tokens
- `llm.token_count.completion` - Output tokens
- `llm.token_count.total` - Total tokens
- `llm.token_count.cache_read` - Cached tokens read
- `llm.token_count.cache_creation` - Cached tokens created

### Metadata
- `session.id` - Session identifier
- `user.id` - User identifier
- `metadata` - Custom metadata dict
- `tag.tags` - Tags array

### Prompt Templates
- `llm.prompt_template.template` - Template string
- `llm.prompt_template.version` - Template version
- `llm.prompt_template.variables` - Template variables

## Semantic Conventions

traceAI supports multiple naming conventions:

```python
from fi_instrumentation.fi_types import SemanticConvention

trace_provider = register(
    semantic_convention=SemanticConvention.FI  # Default
)
```

| Convention | Description |
|------------|-------------|
| `FI` | Future AGI native (`fi.*` namespace) |
| `OTEL_GENAI` | OpenTelemetry GenAI SIG |
| `OPENINFERENCE` | Arize Phoenix convention |
| `OPENLLMETRY` | Traceloop convention |

## Transport Options

Choose between HTTP and gRPC:

```python
from fi_instrumentation.fi_types import Transport

# HTTP (default)
trace_provider = register(
    transport=Transport.HTTP
)

# gRPC (requires grpc extras)
trace_provider = register(
    transport=Transport.GRPC
)
```

## Error Handling

Errors are automatically captured with context:

```python
try:
    response = client.chat.completions.create(...)
except Exception as e:
    # Error is automatically:
    # - Recorded on the span
    # - Status set to ERROR
    # - Exception details captured
    raise
```

Span error attributes:
- `otel.status_code` - ERROR or OK
- `otel.status_description` - Error message
- `exception.type` - Exception class name
- `exception.message` - Error message
- `exception.stacktrace` - Stack trace

## Related

- [Context Managers](context-managers.md) - Adding metadata
- [TraceConfig](../configuration/trace-config.md) - Privacy settings
- [Evaluation Tags](../configuration/eval-tags.md) - AI evaluations
