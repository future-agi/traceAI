# traceAI for TypeScript and JavaScript

OpenTelemetry instrumentation for Node.js AI applications. The core is `@traceai/fi-core`; add an integration package for the client or framework you use.

[All languages](../README.md) · [Integration guides](https://docs.futureagi.com/docs/integrations/traceai) · [Core SDK](packages/fi-core/)

## Quickstart

The OpenAI instrumentor supports OpenAI SDK 4 and 5. This example installs SDK 5 and runs TypeScript with `tsx`.

```bash
npm install @traceai/fi-core @traceai/openai @opentelemetry/instrumentation openai@5
npm install --save-dev tsx typescript @types/node
```

```bash
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
export OPENAI_API_KEY="your-openai-api-key"
export OPENAI_MODEL="gpt-4.1"  # Choose a model available to your account.
```

Save as `quickstart.ts`, then run `npx tsx quickstart.ts`.

```typescript
import OpenAI from "openai";
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

const provider = register({
  projectName: "traceai-quickstart",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new OpenAIInstrumentation();
registerInstrumentations({ tracerProvider: provider, instrumentations: [instrumentation] });
instrumentation.manuallyInstrument(OpenAI); // Explicit patch for ESM imports.

async function main() {
  try {
    const response = await new OpenAI().chat.completions.create({
      model: process.env.OPENAI_MODEL!,
      messages: [{ role: "user", content: "Say hello in one word." }],
    });
    console.log(response.choices[0].message.content);
  } finally {
    await provider.shutdown();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
```

Open the `traceai-quickstart` project in **Observe** in [Future AGI](https://app.futureagi.com). Always await `provider.shutdown()` in short-lived scripts.

## Module loading

- **ESM:** the explicit `manuallyInstrument(OpenAI)` call patches an already-imported module, as in the example above.
- **CommonJS:** register instrumentation before `require("openai")`, so the module hook patches it automatically. Omit the manual patch when using the hook.
- Use only one patching approach for a given module instance.

[OpenAI package](packages/traceai_openai/) · [Instrumentation implementation](packages/traceai_openai/src/instrumentation.ts)

## Context, export, and privacy

The instrumentors use OpenTelemetry spans and accept your tracer provider through `registerInstrumentations`. Supply your own provider and exporter to use another destination; the `register()` helper configures Future AGI.

Content is captured by default. The core's `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, message/text/image hide flags, `FI_HIDE_EMBEDDING_VECTORS`, and `FI_HIDE_ERROR_DETAILS` let you limit captured content. Set the flags before initialization. When the error flag is unset, input/output value, message, and text hide flags also hide error details.

[Privacy controls](../README.md#privacy-controls) · [TraceConfig implementation](packages/fi-core/src/trace/trace-config/traceConfig.ts) · [Semantic conventions](packages/fi-semantic-conventions/)

## Packages

<details>
<summary><strong>Browse the complete TypeScript package catalog</strong></summary>

| Package | Source |
| :--- | :--- |
| `@traceai/fi-core` | [fi-core](packages/fi-core/) |
| `@traceai/fi-semantic-conventions` | [fi-semantic-conventions](packages/fi-semantic-conventions/) |
| `@traceai/anthropic` | [traceai_anthropic](packages/traceai_anthropic/) |
| `@traceai/bedrock` | [traceai_bedrock](packages/traceai_bedrock/) |
| `@traceai/beeai` | [traceai_beeai](packages/traceai_beeai/) |
| `@traceai/cerebras` | [traceai_cerebras](packages/traceai_cerebras/) |
| `@traceai/chromadb` | [traceai_chromadb](packages/traceai_chromadb/) |
| `@traceai/claude-agent-sdk` | [traceai_claude_agent_sdk](packages/traceai_claude_agent_sdk/) |
| `@traceai/codex-sdk` | [traceai_codex_sdk](packages/traceai_codex_sdk/) |
| `@traceai/cohere` | [traceai_cohere](packages/traceai_cohere/) |
| `@traceai/cursor-sdk` | [traceai_cursor_sdk](packages/traceai_cursor_sdk/) |
| `@traceai/deepseek` | [traceai_deepseek](packages/traceai_deepseek/) |
| `@traceai/fireworks` | [traceai_fireworks](packages/traceai_fireworks/) |
| `@traceai/genkit` | [traceai_genkit](packages/traceai_genkit/) |
| `@traceai/fi-instrumentation-google-adk` | [traceai_google_adk](packages/traceai_google_adk/) |
| `@traceai/google-genai` | [traceai_google_genai](packages/traceai_google_genai/) |
| `@traceai/groq` | [traceai_groq](packages/traceai_groq/) |
| `@traceai/guardrails` | [traceai_guardrails](packages/traceai_guardrails/) |
| `@traceai/huggingface` | [traceai_huggingface](packages/traceai_huggingface/) |
| `@traceai/instructor` | [traceai_instructor](packages/traceai_instructor/) |
| `@traceai/lancedb` | [traceai_lancedb](packages/traceai_lancedb/) |
| `@traceai/langchain` | [traceai_langchain](packages/traceai_langchain/) |
| `@traceai/fi-instrumentation-livekit` | [traceai_livekit](packages/traceai_livekit/) |
| `@traceai/llamaindex` | [traceai_llamaindex](packages/traceai_llamaindex/) |
| `@traceai/mastra` | [traceai_mastra](packages/traceai_mastra/) |
| `@traceai/mcp` | [traceai_mcp](packages/traceai_mcp/) |
| `@traceai/milvus` | [traceai_milvus](packages/traceai_milvus/) |
| `@traceai/mistral` | [traceai_mistral](packages/traceai_mistral/) |
| `@traceai/mongodb` | [traceai_mongodb](packages/traceai_mongodb/) |
| `@traceai/ollama` | [traceai_ollama](packages/traceai_ollama/) |
| `@traceai/openai` | [traceai_openai](packages/traceai_openai/) |
| `@traceai/openai-agents` | [traceai_openai_agents](packages/traceai_openai_agents/) |
| `@traceai/pgvector` | [traceai_pgvector](packages/traceai_pgvector/) |
| `@traceai/pi-coding-agent` | [traceai_pi_coding_agent](packages/traceai_pi_coding_agent/) |
| `@traceai/pinecone` | [traceai_pinecone](packages/traceai_pinecone/) |
| `@traceai/fi-instrumentation-pipecat` | [traceai_pipecat](packages/traceai_pipecat/) |
| `@traceai/portkey` | [traceai_portkey](packages/traceai_portkey/) |
| `@traceai/qdrant` | [traceai_qdrant](packages/traceai_qdrant/) |
| `@traceai/redis` | [traceai_redis](packages/traceai_redis/) |
| `@traceai/strands` | [traceai_strands](packages/traceai_strands/) |
| `@traceai/together` | [traceai_together](packages/traceai_together/) |
| `@traceai/vercel` | [traceai_vercel](packages/traceai_vercel/) |
| `@traceai/fi-instrumentation-vertexai` | [traceai_vertexai](packages/traceai_vertexai/) |
| `@traceai/vllm` | [traceai_vllm](packages/traceai_vllm/) |
| `@traceai/voltagent` | [traceai_voltagent](packages/traceai_voltagent/) |
| `@traceai/weaviate` | [traceai_weaviate](packages/traceai_weaviate/) |
| `@traceai/xai` | [traceai_xai](packages/traceai_xai/) |

</details>

## Contributing and license

Read the [contributing guide](../CONTRIBUTING.md). Licensed under [Apache 2.0](../LICENSE).
