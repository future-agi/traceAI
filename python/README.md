# traceAI Python SDK

The Python SDK for traceAI provides OpenTelemetry-native instrumentation for AI applications.

## Installation

Install the core instrumentation library and your framework of choice:

```bash
# Core library (required)
pip install fi-instrumentation

# Framework-specific instrumentation
pip install traceai-openai        # For OpenAI
pip install traceai-anthropic     # For Anthropic
pip install traceai-langchain     # For LangChain
pip install traceai-llamaindex    # For LlamaIndex
# ... see full list below
```

## Quick Start

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
import openai

# Set environment variables
os.environ["FI_API_KEY"] = "<your-api-key>"
os.environ["FI_SECRET_KEY"] = "<your-secret-key>"

# Register tracer provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my_app"
)

# Instrument your framework
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

# Use as normal - tracing happens automatically
client = openai.OpenAI()
response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

## Core Concepts

### Project Types

- **`ProjectType.OBSERVE`**: For production monitoring. Cannot use eval tags.
- **`ProjectType.EXPERIMENT`**: For development/testing. Supports eval tags for AI evaluations.

### TraceConfig (Privacy Controls)

Control what data gets captured:

```python
from fi_instrumentation.instrumentation.config import TraceConfig

config = TraceConfig(
    hide_inputs=False,              # Hide all input values
    hide_outputs=False,             # Hide all output values
    hide_input_messages=False,      # Hide input messages only
    hide_output_messages=False,     # Hide output messages only
    hide_input_images=False,        # Hide images in inputs
    hide_embedding_vectors=False,   # Hide embedding vectors
    base64_image_max_length=32000,  # Truncate large images
)
```

Or use environment variables:
- `FI_HIDE_INPUTS=true`
- `FI_HIDE_OUTPUTS=true`
- `FI_HIDE_INPUT_MESSAGES=true`
- `FI_HIDE_OUTPUT_MESSAGES=true`
- `FI_HIDE_INPUT_IMAGES=true`
- `FI_HIDE_EMBEDDING_VECTORS=true`

### Context Managers

Add metadata to spans:

```python
from fi_instrumentation import using_attributes

with using_attributes(
    session_id="session-123",
    user_id="user-456",
    metadata={"environment": "production"},
    tags=["chat", "support"]
):
    response = client.chat.completions.create(...)
```

Available context managers:
- `using_session(session_id)` - Track session
- `using_user(user_id)` - Track user
- `using_metadata(dict)` - Add custom metadata
- `using_tags(list)` - Add categorical tags
- `using_prompt_template(template, version, variables)` - Track prompt variants
- `using_attributes(...)` - Combined context manager
- `suppress_tracing()` - Temporarily disable tracing

### Evaluation Tags (Experiments Only)

Run automated evaluations on spans:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    ProjectType, EvalTag, EvalTagType,
    EvalSpanKind, EvalName, ModelChoices
)

eval_tags = [
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.CONTEXT_ADHERENCE,
        custom_eval_name="my_context_check",
        mapping={
            "context": "raw.input",
            "output": "raw.output"
        },
        model=ModelChoices.TURING_SMALL
    )
]

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="my_experiment",
    eval_tags=eval_tags
)
```

**Available Evaluations (60+):**

| Category | Evaluations |
|----------|-------------|
| Content Quality | `CONTEXT_ADHERENCE`, `COMPLETENESS`, `GROUNDEDNESS`, `SUMMARY_QUALITY` |
| Safety | `TOXICITY`, `PII`, `CONTENT_MODERATION`, `PROMPT_INJECTION` |
| Accuracy | `FACTUAL_ACCURACY`, `CONTEXT_RELEVANCE`, `DETECT_HALLUCINATION` |
| Bias | `BIAS_DETECTION`, `NO_RACIAL_BIAS`, `NO_GENDER_BIAS` |
| Format | `IS_JSON`, `IS_CODE`, `ONE_LINE`, `CONTAINS_VALID_LINK` |
| Similarity | `BLEU_SCORE`, `ROUGE_SCORE`, `EMBEDDING_SIMILARITY` |

## Supported Frameworks

### LLM Providers

| Package | Framework |
|---------|-----------|
| `traceai-openai` | OpenAI |
| `traceai-anthropic` | Anthropic |
| `traceai-mistralai` | Mistral AI |
| `traceai-groq` | Groq |
| `traceai-vertexai` | Google Vertex AI |
| `traceai-google-genai` | Google Generative AI |
| `traceai-google-adk` | Google ADK |
| `traceai-bedrock` | AWS Bedrock |
| `traceai-litellm` | LiteLLM |
| `traceai-portkey` | Portkey |

### Agent Frameworks

| Package | Framework |
|---------|-----------|
| `traceai-langchain` | LangChain |
| `traceai-llamaindex` | LlamaIndex |
| `traceai-crewai` | CrewAI |
| `traceai-autogen` | AutoGen |
| `traceai-openai-agents` | OpenAI Agents |
| `traceai-smolagents` | Smol Agents |
| `traceai-dspy` | DSPy |
| `traceai-haystack` | Haystack |

### Tools & Integrations

| Package | Framework |
|---------|-----------|
| `traceai-instructor` | Instructor |
| `traceai-guardrails` | Guardrails AI |
| `traceai-mcp` | Model Context Protocol |
| `traceai-pipecat` | Pipecat |
| `traceai-livekit` | LiveKit |

### Vector Databases

| Package | Database |
|---------|----------|
| `traceai-pinecone` | Pinecone |
| `traceai-chromadb` | ChromaDB |
| `traceai-qdrant` | Qdrant |
| `traceai-weaviate` | Weaviate |
| `traceai-milvus` | Milvus |
| `traceai-lancedb` | LanceDB |
| `traceai-mongodb` | MongoDB Atlas Vector |
| `traceai-pgvector` | pgvector |
| `traceai-redis` | Redis Vector |

## Environment Variables

### Authentication
- `FI_API_KEY` - API key for Future AGI
- `FI_SECRET_KEY` - Secret key for Future AGI

### Endpoints
- `FI_BASE_URL` - HTTP collector endpoint (default: `https://api.futureagi.com`)
- `FI_GRPC_URL` - gRPC collector endpoint (default: `https://grpc.futureagi.com`)

### Project
- `FI_PROJECT_NAME` - Default project name
- `FI_PROJECT_VERSION_NAME` - Default version name

### Performance
- `OTEL_BSP_SCHEDULE_DELAY` - Batch export delay (ms)
- `OTEL_BSP_MAX_QUEUE_SIZE` - Max queue size
- `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` - Max batch size
- `OTEL_BSP_EXPORT_TIMEOUT` - Export timeout (ms)

## Advanced Usage

### Custom TracerProvider

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from traceai_openai import OpenAIInstrumentor

# Create custom provider
provider = TracerProvider()
trace.set_tracer_provider(provider)

# Add custom exporter
exporter = OTLPSpanExporter(
    endpoint="https://your-collector.com/v1/traces",
    headers={"Authorization": "Bearer your-token"}
)
provider.add_span_processor(BatchSpanProcessor(exporter))

# Instrument
OpenAIInstrumentor().instrument(tracer_provider=provider)
```

### Semantic Conventions

traceAI supports multiple semantic conventions:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import SemanticConvention

trace_provider = register(
    project_name="my_app",
    semantic_convention=SemanticConvention.FI  # Default
    # Or: SemanticConvention.OTEL_GENAI
    # Or: SemanticConvention.OPENINFERENCE
    # Or: SemanticConvention.OPENLLMETRY
)
```

### Transport Options

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import Transport

# HTTP (default)
trace_provider = register(
    project_name="my_app",
    transport=Transport.HTTP
)

# gRPC (requires grpc extras)
trace_provider = register(
    project_name="my_app",
    transport=Transport.GRPC
)
```

## Examples

See framework-specific examples in each package:
- [OpenAI Examples](frameworks/openai/examples/)
- [Anthropic Examples](frameworks/anthropic/examples/)
- [LangChain Examples](frameworks/langchain/examples/)
- [CrewAI Examples](frameworks/crewai/examples/)

## Documentation

- [Full Documentation](https://docs.futureagi.com/)
- [API Reference](https://docs.futureagi.com/api)
- [Cookbooks](https://docs.futureagi.com/cookbook)

## License

GPL-3.0 License
