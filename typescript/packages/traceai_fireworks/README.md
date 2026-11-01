# @traceai/fireworks (deprecated)

Trace Fireworks AI with [`@traceai/openai`](../traceai_openai/README.md). Fireworks serves an [OpenAI-compatible API](https://docs.fireworks.ai/tools-sdks/openai-compatibility), so the shared OpenAI instrumentor traces it and sets `gen_ai.provider.name=fireworks` for requests sent to `api.fireworks.ai`.

`FireworksInstrumentation` remains available for existing applications and delegates to the shared instrumentor. Constructing it emits one `DeprecationWarning` (code `TRACEAI_FIREWORKS_DEPRECATED`) per process.

## Set up Fireworks tracing

```bash
npm install @traceai/openai @traceai/fi-core openai
export FIREWORKS_API_KEY="your-fireworks-api-key"
export FIREWORKS_MODEL="accounts/fireworks/models/your-model-id"
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
```

Choose a model available to your Fireworks account. Save as `fireworks.mts` and run `node fireworks.mts` with Node.js 22.18 or later:

```typescript
import OpenAI from "openai";
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";

const model = process.env.FIREWORKS_MODEL;
if (!model) throw new Error("Set FIREWORKS_MODEL to a Fireworks model ID.");

const provider = register({
  projectName: "fireworks-app",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new OpenAIInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(OpenAI);

const client = new OpenAI({
  apiKey: process.env.FIREWORKS_API_KEY,
  baseURL: "https://api.fireworks.ai/inference/v1",
});
try {
  const response = await client.chat.completions.create({
    model,
    messages: [{ role: "user", content: "Say hello in one sentence." }],
  });
  console.log(response.choices[0].message.content);
} finally {
  instrumentation.disable();
  await provider.shutdown();
}
```

Open **Observe → fireworks-app** to see an **OpenAI Chat Completions** span with `gen_ai.provider.name=fireworks`, the model, latency and reported token usage. Chat completions, legacy completions and embeddings are traced, including streams and failed requests. Fully consume streams before shutting down the provider.

For CommonJS, you can instead construct the instrumentation before `require("openai")` and skip `manuallyInstrument`. To keep prompts, completions and error messages out of telemetry, pass `traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true }` to `OpenAIInstrumentation`.

## Migrate from FireworksInstrumentation

1. Install `@traceai/openai` and replace `FireworksInstrumentation` with `OpenAIInstrumentation`. The constructor options, `setTracerProvider`, `manuallyInstrument` and `disable` are the same.
2. Remove `@traceai/fireworks` from your dependencies.

Behavior to expect after upgrading `@traceai/fireworks`, before or after migrating:

- Every OpenAI SDK client in the process is traced, not only Fireworks clients. Each span's provider comes from its client's API host. Unknown hosts, such as custom proxies, keep the `openai` fallback.
- Span names are `OpenAI Chat Completions`, `OpenAI Completions` and `OpenAI Embeddings`. The instrumentation scope is the shared OpenAI instrumentor.
- If both `FireworksInstrumentation` and `OpenAIInstrumentation` are enabled, each request produces one span. The instrumentor that patched the SDK first applies its tracer provider and `traceConfig`. Disable both at shutdown.
- The supported OpenAI SDK range is `^4.0.0 || ^5.0.0 || ^6.0.0 || ^7.0.0`.

## License

Apache-2.0
