# traceAI for Python

OpenTelemetry instrumentation for AI applications. Install the core and the instrumentor for the framework you use.

[All languages](../README.md) · [Integration guides](https://docs.futureagi.com/docs/integrations/traceai) · [Core API and configuration](fi_instrumentation/README.md)

## Supported Python versions

Core tracing supports Python 3.10–3.15, with either the default HTTP exporter
or the optional `grpc` exporter:

```bash
pip install fi-instrumentation-otel         # HTTP tracing, Python 3.10–3.15
pip install 'fi-instrumentation-otel[grpc]' # gRPC tracing, Python 3.10–3.15
```

The optional `evaluation` extra (including `[grpc,evaluation]`) supports Python
3.10–3.14. Its dependency chain, including LiteLLM/fastuuid, is not yet compatible
with Python 3.15. Use Python 3.14 or earlier for
`pip install 'fi-instrumentation-otel[evaluation]'`; installing this extra on
3.15 can fail while resolving or building those dependencies. It is never
silently omitted, and it does not restrict core-only or `[grpc]` installs.
Framework instrumentors have their own runtime constraints.

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

## Zero-code tracing

To trace an existing app without changing its code, install the traceAI package for each library it uses, set the keys, and start the app through `fi-instrument`:

```bash
pip install fi-instrumentation-otel traceai-openai
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
fi-instrument python app.py
```

Every installed traceAI integration turns on when the app first imports its library. To keep your start command, use `FI_AUTO_INSTRUMENT=true python app.py` or `FI_AUTO_INSTRUMENT=true opentelemetry-instrument python app.py`; in code, the same is one call to `fi_instrumentation.init()`. Future AGI receives only AI spans; your other OpenTelemetry exporters keep receiving every span. A library without its traceAI package is not traced: `fi-instrument doctor` shows what is covered, and `fi-instrument bootstrap --install` adds the missing packages. The `deepseek`, `fireworks`, `xai`, `minimax` and `vllm` integrations are on by default as well; they share the OpenAI SDK with the `openai` integration, and each model call still produces exactly one span. See the [zero-code guide](../docs/python/auto-instrumentation.md).

If the app already calls `register()` and `instrument()` itself, do not also start it with `fi-instrument` or `FI_AUTO_INSTRUMENT`: the agent would register its own provider first, and your calls would have no effect. Remove those calls, or keep them and call `fi_instrumentation.init()` after them.

For a provider the agent creates, `FI_SAMPLE_RATE` (or `init(sample_rate=...)` / `sample_rate` in `futureagi.toml`) wins over `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG`; otherwise those standard OpenTelemetry settings choose the head sampler. The default is `ParentBased(TraceIdRatioBased(1.0))`; invalid or unsupported sampler settings use that default with one warning. An explicit `sampler=` still takes precedence. An application-owned provider always keeps its own sampler, including its parent sampled-flag behaviour.

On agent-created providers, sampled spans decided locally at a ratio strictly between 0 and 1 carry the numeric `fi.sampling.ratio` keep probability and write `fi=r:<ratio>` in W3C tracestate, preserving other entries up to its 32-entry limit. Parent-based samplers copy a sampled remote or local parent's valid `fi` ratio (finite and strictly between 0 and 1), regardless of the local root rate. This includes the default configuration, `FI_SAMPLE_RATE=1`, `parentbased_always_on`, `parentbased_always_off`, and `parentbased_traceidratio` at any rate. The attribute is restored on the Future AGI export copy if SDK attribute limits evict it, without changing those limits or the application's spans; absence means `1.0` for downstream estimates. Roots decided at rate 1 add no metadata. Non-parent-based samplers never copy an upstream ratio (`traceidratio` still stamps its own fractional rate). Parent-based children with unsampled parents or missing/invalid parent metadata receive no added sampling metadata. Dropped or record-only spans, explicit custom samplers, and application-owned providers also receive no added sampling metadata.

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

The wrapper emits one `lambda.handler` server span with the invocation ID, trigger and cold-start flag. It imports only W3C `traceparent` from API Gateway v1/v2, function URL and ALB events (including multi-value headers). Other events start fresh traces. The invocation wrapper never captures event/result content, request URLs, headers, exception messages or stacks; errors carry only their type. For HTTP triggers it reads only the integer `statusCode` of the returned response: the span records `http.response.status_code`, and a 5xx response marks it as an error with `error.type` set to the code, so frameworks that turn exceptions into 500 responses (Mangum, for example) still report failed requests. Child instrumentors retain their own privacy settings.

After the span ends, it waits up to two seconds for a flush, capped by the remaining invocation time minus 250 ms. Configure those limits with `flush_timeout_millis` and `reserve_millis`. A timed-out flush is **not cancelled** and may continue; later invocations skip flushing while it is in flight, preventing additional flush workers from accumulating. Delivery remains best effort. Use a batch processor and a finite exporter timeout; synchronous span processors can block inside span end, outside this wait budget.

Remove any copied Lambda tracing helper, manual invocation span or explicit flush when adopting the wrapper. Do not combine it with `AwsLambdaInstrumentor` or an auto-instrumenting Lambda layer. Do not shut down the shared provider after each invocation. The old example's `LAMBDA_TRACE_CAPTURE_CONTENT` switch does not apply to this metadata-only wrapper.

## Context and privacy

Use `using_attributes(session_id=..., user_id=..., metadata=...)` to attach application context. The core also provides `using_session`, `using_user`, `using_tags`, and `suppress_tracing`.

Content capture is on by default. Configure `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`, and `FI_HIDE_EMBEDDING_VECTORS` before initialization. Set each to `true` to hide that content.

`FI_HIDE_ERROR_DETAILS=true` hides error descriptions, exception messages, and stack traces while keeping status and error types. When unset, it follows the input/output value, message, and text hide flags. Explicit `false` overrides the derived value. See the [configuration implementation](fi_instrumentation/instrumentation/config.py) and the [cross-language privacy guide](../README.md#privacy-controls).

## Use your OpenTelemetry pipeline

Pass your own OpenTelemetry `TracerProvider` to `OpenAIInstrumentor().instrument(tracer_provider=provider)` to use an existing exporter. The `register()` helper configures Future AGI by default. On current source, `register(span_exporter=...)` replaces that destination; older published versions may not include this option.

### OTLP headers from the environment

`OTEL_EXPORTER_OTLP_TRACES_HEADERS` and `OTEL_EXPORTER_OTLP_HEADERS` describe the collector you configured for OpenTelemetry, which may be a different backend. The exporters traceAI creates for Future AGI (`HTTPSpanExporter` and `GRPCSpanExporter` whose resolved endpoint is the default or the `FI_BASE_URL` / `FI_TRACES_URL` / `FI_GRPC_URL` host, `register()` and zero-code auto-instrumentation) send the Future AGI keys and the headers you pass in; OTLP headers from the environment are not included. Headers are settled when the exporter is created, not at export time. An exporter you point at another endpoint yourself, such as `HTTPSpanExporter(endpoint="https://collector.example/v1/traces")`, and any OpenTelemetry exporter you build, keep standard OpenTelemetry behaviour: they use the environment headers and do not receive `FI_API_KEY` / `FI_SECRET_KEY`. If an OpenTelemetry exporter release keeps its headers in a layout this release cannot set, an exporter bound for Future AGI raises `UnsupportedOTLPExporterError` instead of sending; the supported exporter versions are 1.29.0 to 1.45.x.

[Provider setup](fi_instrumentation/otel.py) · [Examples](examples/)

## Troubleshooting: 401 Unauthorized when exporting traces

- **Check the export URL.** Set `FI_BASE_URL` to the deployment you intend to use. The default HTTP exporter sends to `/tracer/v1/traces` and replaces any path in `FI_BASE_URL`; an explicit `HTTPSpanExporter(endpoint=...)` is used unchanged. A successful health check does not validate your export credentials.
- **Use one pair of keys.** Set `FI_API_KEY` and `FI_SECRET_KEY` from the same key, in the exporting process, before the exporter is created. Do not use a masked secret.
- **Use one credential source.** Explicit `Authorization`, `X-Api-Key` or `X-Secret-Key` headers take precedence and the environment keys are then not added. Remove an unrelated `Authorization` header rather than mixing sources. An empty credential header is ignored.
- **Retired or disabled keys.** If the service reports a retired key, create a personal key at `/dashboard/keys`, and confirm that its owner still has access to the organization (and workspace, when scoped).
- **When reporting an issue**, share the sanitized URL, SDK and exporter versions, the response status, error code and request id, and the header *names* you send. Never share key values, `Authorization` values or an unredacted environment dump.

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
| `traceAI-agentspec` | [agentspec](frameworks/agentspec/) |
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
| `traceAI-google-generativeai` | [google-generativeai](frameworks/google-generativeai/) |
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
| `traceAI-elasticsearch` | [elasticsearch](frameworks/elasticsearch/) |
| `traceAI-marqo` | [marqo](frameworks/marqo/) |
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
| `traceAI-promptflow` | [promptflow](frameworks/promptflow/) |
| `traceai-pydantic-ai` | [pydantic-ai](frameworks/pydantic-ai/) |
| `traceAI-qdrant` | [qdrant](frameworks/qdrant/) |
| `traceAI-redis` | [redis-vector](frameworks/redis-vector/) |
| `traceAI-replicate` | [replicate](frameworks/replicate/) |
| `traceAI-sagemaker` | [sagemaker](frameworks/sagemaker/) |
| `traceAI-watsonx` | [watsonx](frameworks/watsonx/) |
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
| `traceAI-writer` | [writer](frameworks/writer/) |
| `traceAI-alephalpha` | [alephalpha](frameworks/alephalpha/) |
| `traceAI-xai` | [xai](frameworks/xai/) |

</details>

## Contributing and license

Read the [contributing guide](../CONTRIBUTING.md). Licensed under [Apache 2.0](../LICENSE).
