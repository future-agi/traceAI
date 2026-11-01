# traceAI for Python

OpenTelemetry instrumentation for AI applications. Install the core and the instrumentor for the framework you use.

[All languages](../README.md) · [Integration guides](https://docs.futureagi.com/docs/integrations/traceai) · [Core API and configuration](fi_instrumentation/README.md)

## Quickstart

Use Python 3.10–3.13 for this OpenAI example.

```bash
pip install fi-instrumentation-otel traceai-openai
```

```bash
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
export OPENAI_API_KEY="your-openai-api-key"
export OPENAI_MODEL="gpt-4.1"  # Choose a model available to your account.
```

Save as `quickstart.py`, then run `python quickstart.py`.

```python
import os
from openai import OpenAI
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor

provider = register(project_name="traceai-quickstart", project_type=ProjectType.OBSERVE)
OpenAIInstrumentor().instrument(tracer_provider=provider)
try:
    response = OpenAI().chat.completions.create(
        model=os.environ["OPENAI_MODEL"],
        messages=[{"role": "user", "content": "Say hello in one word."}],
    )
    print(response.choices[0].message.content)
finally:
    provider.shutdown()  # Flush pending spans before the process exits.
```

Open the `traceai-quickstart` project in **Observe** in [Future AGI](https://app.futureagi.com). `provider.shutdown()` flushes pending spans before exit.

## Shutdown on termination

From `fi-instrumentation-otel` 1.2.2, `register()` leaves your SIGINT and SIGTERM handlers unchanged. Default SIGTERM does not run Python exit hooks, so call `provider.shutdown()` from your application's shutdown path to export pending spans on container stop, and allow enough termination grace time. SIGKILL cannot flush spans.

A standalone script that owns its signal policy can opt in:

```python
import signal

def stop(signum, frame):
    provider.shutdown()
    raise SystemExit(0)  # Your application chooses the exit status.

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
```

In a web server, worker or async service, use the framework's shutdown hook instead of replacing its handlers: stop accepting work, finish or cancel active requests, then call `provider.shutdown()`. `force_flush()` exports ended spans without shutting down the provider; it does not replace shutdown for operations still running.

## AWS Lambda invocation tracing

The core SDK provides `fi_instrumentation.aws_lambda.LambdaTracer`; no separate Lambda instrumentor package is needed. Create one instance outside your synchronous handler and share its provider with model-client instrumentors:

```python
from fi_instrumentation import register
from fi_instrumentation.aws_lambda import LambdaTracer
from fi_instrumentation.fi_types import ProjectType

provider = register(
    project_name="lambda-orders", project_type=ProjectType.OBSERVE,
    batch=True, schedule_delay_millis=60_000, timeout=2, verbose=False,
)
tracing = LambdaTracer(provider)

@tracing.wrap
def lambda_handler(event, context):
    return {"statusCode": 200, "body": "Order accepted"}
```

The wrapper emits one `lambda.handler` server span with the invocation ID, trigger and cold-start flag. It imports only W3C `traceparent` from API Gateway v1/v2, function URL and ALB events (including multi-value headers). Other events start fresh traces. The invocation wrapper never captures event/result content, request URLs, headers, exception messages or stacks; errors carry only their type. Child instrumentors retain their own privacy settings.

After the span ends, it waits up to two seconds for a flush, capped by the remaining invocation time minus 250 ms. Configure those limits with `flush_timeout_millis` and `reserve_millis`. A timed-out flush is **not cancelled** and may continue; later invocations skip flushing while it is in flight, preventing additional flush workers from accumulating. Delivery remains best effort. Use a batch processor and a finite exporter timeout; synchronous span processors can block inside span end, outside this wait budget.

Remove any copied Lambda tracing helper, manual invocation span or explicit flush when adopting the wrapper. Do not combine it with `AwsLambdaInstrumentor` or an auto-instrumenting Lambda layer. Do not shut down the shared provider after each invocation. The old example's `LAMBDA_TRACE_CAPTURE_CONTENT` switch does not apply to this metadata-only wrapper.

## Context and privacy

Use `using_attributes(session_id=..., user_id=..., metadata=...)` to attach application context. The core also provides `using_session`, `using_user`, `using_tags`, and `suppress_tracing`.

Content capture is on by default. Configure `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`, and `FI_HIDE_EMBEDDING_VECTORS` before initialization. Set each to `true` to hide that content.

`FI_HIDE_ERROR_DETAILS=true` hides error descriptions, exception messages, and stack traces while keeping status and error types. When unset, it follows the input/output value, message, and text hide flags. Explicit `false` overrides the derived value. See the [configuration implementation](fi_instrumentation/instrumentation/config.py) and the [cross-language privacy guide](../README.md#privacy-controls).

## Use your OpenTelemetry pipeline

Pass your own OpenTelemetry `TracerProvider` to `OpenAIInstrumentor().instrument(tracer_provider=provider)` to use an existing exporter. The `register()` helper configures Future AGI by default. On current source, `register(span_exporter=...)` replaces that destination; older published versions may not include this option.

[Provider setup](fi_instrumentation/otel.py) · [Examples](examples/)

## Packages

Each package below links to its own installation instructions, supported SDK versions, and examples. Package availability and framework support are independent; do not assume every package supports the same upstream version.

<details>
<summary><strong>Browse the complete Python instrumentor catalog</strong></summary>

| Package | Source |
| :--- | :--- |
| `traceAI-a2a` | [a2a](frameworks/a2a/) |
| `traceAI-ag2` | [ag2](frameworks/ag2/) |
| `traceAI-ag2-classic` | [ag2-classic](frameworks/ag2-classic/) |
| `traceAI-agent-framework` | [agent-framework](frameworks/agent-framework/) |
| `traceai-agno` | [agno](frameworks/agno/) |
| `traceAI-anthropic` | [anthropic](frameworks/anthropic/) |
| `traceAI-autogen` | [autogen](frameworks/autogen/) |
| `traceAI-bedrock` | [bedrock](frameworks/bedrock/) |
| `traceai-beeai` | [beeai](frameworks/beeai/) |
| `traceAI-cerebras` | [cerebras](frameworks/cerebras/) |
| `traceAI-chromadb` | [chromadb](frameworks/chromadb/) |
| `traceai-claude-agent-sdk` | [claude-agent-sdk](frameworks/claude-agent-sdk/) |
| `traceai-cognee` | [cognee](frameworks/cognee/) |
| `traceAI-cohere` | [cohere](frameworks/cohere/) |
| `traceAI-crewai` | [crewai](frameworks/crewai/) |
| `traceAI-deepseek` | [deepseek](frameworks/deepseek/) |
| `traceAI-discoveryengine` | [discoveryengine](frameworks/discoveryengine/) |
| `traceAI-DSPy` | [dspy](frameworks/dspy/) |
| `traceAI-elevenlabs-agents` | [elevenlabs-agents](frameworks/elevenlabs-agents/) |
| `traceAI-exa` | [exa](frameworks/exa/) |
| `traceAI-firecrawl` | [firecrawl](frameworks/firecrawl/) |
| `traceAI-fireworks` | [fireworks](frameworks/fireworks/) |
| `traceAI-google-adk` | [google-adk](frameworks/google-adk/) |
| `traceAI-google-genai` | [google-genai](frameworks/google-genai/) |
| `traceAI-google-genai-live` | [google-genai-live](frameworks/google-genai-live/) |
| `traceAI-groq` | [groq](frameworks/groq/) |
| `traceAI-guardrails` | [guardrails](frameworks/guardrails/) |
| `traceAI-haystack` | [haystack](frameworks/haystack/) |
| `traceAI-hermes-agent` | [hermes-agent](frameworks/hermes-agent/) |
| `traceAI-huggingface` | [huggingface](frameworks/huggingface/) |
| `traceAI-instructor` | [instructor](frameworks/instructor/) |
| `traceAI-lancedb` | [lancedb](frameworks/lancedb/) |
| `traceAI-langchain` | [langchain](frameworks/langchain/) |
| `traceAI-litellm` | [litellm](frameworks/litellm/) |
| `traceAI-livekit` | [livekit](frameworks/livekit/) |
| `traceAI-llamaindex` | [llama_index](frameworks/llama_index/) |
| `traceAI-mcp` | [mcp](frameworks/mcp/) |
| `traceAI-mcp-use` | [mcp-use](frameworks/mcp-use/) |
| `traceAI-milvus` | [milvus](frameworks/milvus/) |
| `traceAI-minimax` | [minimax](frameworks/minimax/) |
| `traceAI-mistralai` | [mistralai](frameworks/mistralai/) |
| `traceAI-mongodb` | [mongodb-vector](frameworks/mongodb-vector/) |
| `traceAI-ollama` | [ollama](frameworks/ollama/) |
| `traceAI-openai` | [openai](frameworks/openai/) |
| `traceAI-openai-agents` | [openai-agents](frameworks/openai-agents/) |
| `traceAI-openai-realtime` | [openai-realtime](frameworks/openai-realtime/) |
| `traceAI-parallel` | [parallel](frameworks/parallel/) |
| `traceAI-pgvector` | [pgvector](frameworks/pgvector/) |
| `traceAI-pinecone` | [pinecone](frameworks/pinecone/) |
| `traceAI-pipecat` | [pipecat](frameworks/pipecat/) |
| `traceAI-portkey` | [portkey](frameworks/portkey/) |
| `traceai-pydantic-ai` | [pydantic-ai](frameworks/pydantic-ai/) |
| `traceAI-qdrant` | [qdrant](frameworks/qdrant/) |
| `traceAI-redis` | [redis-vector](frameworks/redis-vector/) |
| `traceAI-replicate` | [replicate](frameworks/replicate/) |
| `traceAI-semantic-kernel` | [semantic-kernel](frameworks/semantic-kernel/) |
| `traceAI-smolagents` | [smolagents](frameworks/smolagents/) |
| `traceai-strands` | [strands](frameworks/strands/) |
| `traceAI-tavily` | [tavily](frameworks/tavily/) |
| `traceAI-together` | [together](frameworks/together/) |
| `traceAI-typesafe` | [typesafe](frameworks/typesafe/) |
| `traceAI-vertexai` | [vertexai](frameworks/vertexai/) |
| `traceAI-vllm` | [vllm](frameworks/vllm/) |
| `traceAI-voyage` | [voyage](frameworks/voyage/) |
| `traceAI-weaviate` | [weaviate](frameworks/weaviate/) |
| `traceAI-xai` | [xai](frameworks/xai/) |

</details>

## Contributing and license

Read the [contributing guide](../CONTRIBUTING.md). Licensed under [Apache 2.0](../LICENSE).
