# traceAI Python SDK

The Python SDK for traceAI provides OpenTelemetry-native instrumentation for AI applications.

## Installation

Install the core instrumentation library and your framework of choice:

```bash
# Core library (required)
pip install fi-instrumentation-otel

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
    hide_error_details=None,        # Hide error messages/stacktraces; None = on if any input/output flag is on
)
```

Or use environment variables:
- `FI_HIDE_INPUTS=true`
- `FI_HIDE_OUTPUTS=true`
- `FI_HIDE_INPUT_MESSAGES=true`
- `FI_HIDE_OUTPUT_MESSAGES=true`
- `FI_HIDE_INPUT_IMAGES=true`
- `FI_HIDE_EMBEDDING_VECTORS=true`
- `FI_HIDE_ERROR_DETAILS=true|false` (unset: follows the input/output flags)

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

Package names and links refer to the source in this repository.

### LLM Providers

| Package | Integration |
|---------|-------------|
| [`traceAI-anthropic`](../python/frameworks/anthropic/) | Anthropic |
| [`traceAI-bedrock`](../python/frameworks/bedrock/) | AWS Bedrock |
| [`traceAI-cerebras`](../python/frameworks/cerebras/) | Cerebras |
| [`traceAI-cohere`](../python/frameworks/cohere/) | Cohere |
| [`traceAI-deepseek`](../python/frameworks/deepseek/) | DeepSeek |
| [`traceAI-fireworks`](../python/frameworks/fireworks/) | Fireworks AI |
| [`traceAI-google-genai-live`](../python/frameworks/google-genai-live/) | Gemini Live |
| [`traceAI-google-genai`](../python/frameworks/google-genai/) | Google Gen AI |
| [`traceAI-vertexai`](../python/frameworks/vertexai/) | Google Vertex AI |
| [`traceAI-groq`](../python/frameworks/groq/) | Groq |
| [`traceAI-huggingface`](../python/frameworks/huggingface/) | Hugging Face |
| [`traceAI-litellm`](../python/frameworks/litellm/) | LiteLLM |
| [`traceAI-minimax`](../python/frameworks/minimax/) | MiniMax |
| [`traceAI-mistralai`](../python/frameworks/mistralai/) | Mistral AI |
| [`traceAI-ollama`](../python/frameworks/ollama/) | Ollama |
| [`traceAI-openai`](../python/frameworks/openai/) | OpenAI |
| [`traceAI-openai-realtime`](../python/frameworks/openai-realtime/) | OpenAI Realtime |
| [`traceAI-replicate`](../python/frameworks/replicate/) | Replicate |
| [`traceAI-together`](../python/frameworks/together/) | Together AI |
| [`traceAI-vllm`](../python/frameworks/vllm/) | vLLM |
| [`traceAI-voyage`](../python/frameworks/voyage/) | Voyage AI |
| [`traceAI-xai`](../python/frameworks/xai/) | xAI (Grok) |

### Agent Frameworks

| Package | Integration |
|---------|-------------|
| [`traceAI-ag2`](../python/frameworks/ag2/) | AG2 |
| [`traceAI-ag2-classic`](../python/frameworks/ag2-classic/) | AG2 Classic |
| [`traceAI-a2a`](../python/frameworks/a2a/) | Agent-to-Agent (A2A) |
| [`traceai-agno`](../python/frameworks/agno/) | Agno |
| [`traceAI-autogen`](../python/frameworks/autogen/) | AutoGen |
| [`traceai-strands`](../python/frameworks/strands/) | AWS Strands Agents |
| [`traceai-claude-agent-sdk`](../python/frameworks/claude-agent-sdk/) | Claude Agent SDK |
| [`traceAI-crewai`](../python/frameworks/crewai/) | CrewAI |
| [`traceAI-elevenlabs-agents`](../python/frameworks/elevenlabs-agents/) | ElevenLabs Agents |
| [`traceAI-google-adk`](../python/frameworks/google-adk/) | Google ADK |
| [`traceAI-hermes-agent`](../python/frameworks/hermes-agent/) | Hermes Agent |
| [`traceai-beeai`](../python/frameworks/beeai/) | IBM BeeAI |
| [`traceAI-langchain`](../python/frameworks/langchain/) | LangChain |
| [`traceAI-llamaindex`](../python/frameworks/llama_index/) | LlamaIndex |
| [`traceAI-mcp-use`](../python/frameworks/mcp-use/) | mcp-use |
| [`traceAI-agent-framework`](../python/frameworks/agent-framework/) | Microsoft Agent Framework |
| [`traceAI-semantic-kernel`](../python/frameworks/semantic-kernel/) | Microsoft Semantic Kernel |
| [`traceAI-openai-agents`](../python/frameworks/openai-agents/) | OpenAI Agents |
| [`traceai-pydantic-ai`](../python/frameworks/pydantic-ai/) | Pydantic AI |
| [`traceAI-smolagents`](../python/frameworks/smolagents/) | SmolAgents |

### Tools and Libraries

| Package | Integration |
|---------|-------------|
| [`traceAI-DSPy`](../python/frameworks/dspy/) | DSPy |
| [`traceAI-exa`](../python/frameworks/exa/) | Exa |
| [`traceAI-firecrawl`](../python/frameworks/firecrawl/) | Firecrawl |
| [`traceAI-discoveryengine`](../python/frameworks/discoveryengine/) | Google Discovery Engine (Vertex AI Search) |
| [`traceAI-guardrails`](../python/frameworks/guardrails/) | Guardrails AI |
| [`traceAI-haystack`](../python/frameworks/haystack/) | Haystack |
| [`traceAI-instructor`](../python/frameworks/instructor/) | Instructor |
| [`traceAI-livekit`](../python/frameworks/livekit/) | LiveKit |
| [`traceAI-mcp`](../python/frameworks/mcp/) | Model Context Protocol (MCP) |
| [`traceAI-parallel`](../python/frameworks/parallel/) | Parallel |
| [`traceAI-pipecat`](../python/frameworks/pipecat/) | Pipecat |
| [`traceAI-portkey`](../python/frameworks/portkey/) | Portkey |
| [`traceAI-tavily`](../python/frameworks/tavily/) | Tavily |
| [`traceAI-typesafe`](../python/frameworks/typesafe/) | TypeSafe |

### Vector Databases

| Package | Integration |
|---------|-------------|
| [`traceAI-chromadb`](../python/frameworks/chromadb/) | ChromaDB |
| [`traceAI-lancedb`](../python/frameworks/lancedb/) | LanceDB |
| [`traceAI-milvus`](../python/frameworks/milvus/) | Milvus |
| [`traceAI-mongodb`](../python/frameworks/mongodb-vector/) | MongoDB Atlas Vector Search |
| [`traceAI-pgvector`](../python/frameworks/pgvector/) | pgvector |
| [`traceAI-pinecone`](../python/frameworks/pinecone/) | Pinecone |
| [`traceAI-qdrant`](../python/frameworks/qdrant/) | Qdrant |
| [`traceAI-redis`](../python/frameworks/redis-vector/) | Redis Vector Search |
| [`traceAI-weaviate`](../python/frameworks/weaviate/) | Weaviate |

### Integration recipes

These examples reuse existing instrumentation or configure tracing adapters.

| Recipe | Guide |
|--------|-------|
| Anannas AI | [Setup](examples/anannas-ai/) |
| Azure AI Foundry | [Setup](examples/azure-ai-foundry/) |
| Cognee | [Setup](examples/cognee/) |
| Databricks | [Setup](examples/databricks/) |
| Doubleword | [Setup](examples/doubleword/) |
| EverOS | [Setup](examples/everos/) |
| Gradio | [Setup](examples/gradio/) |
| LangSmith trace forwarding | [Setup](examples/langsmith/) |
| NVIDIA DGX Cloud Lepton | [Setup](examples/lepton/) |
| MLflow | [Setup](examples/mlflow/) |
| OpenInference OTLP forwarding | [Setup](examples/openinference/) |
| OpenInference instrumented application | [Setup](examples/openinference-live/) |
| OpenLIT | [Setup](examples/openlit/) |
| OpenLLMetry (Traceloop SDK) | [Setup](examples/openllmetry/) |
| OrcaRouter | [Setup](examples/orcarouter/) |
| Perplexity Agent API | [Setup](examples/perplexity/) |
| PrismML Bonsai | [Setup](examples/prismml/) |
| Qwen (Alibaba Cloud Model Studio) | [Setup](examples/qwen/) |
| Deep Agents (via LangChain) | [Setup](frameworks/langchain/README.md#deep-agents) |

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

### More span processors

A span processor added to the provider `register()` returns runs next to the Future AGI exporter, so spans go to both:

```python
from fi_instrumentation import register
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

trace_provider = register(project_name="my_project")
trace_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
```

To export somewhere other than Future AGI, pass your exporter to `register(span_exporter=...)`. In older releases (see the CHANGELOG), the first `add_span_processor()` call replaced the Future AGI exporter, and spans stopped reaching Future AGI.

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
