# Zero-code tracing for Node.js and TypeScript

`@traceai/auto` traces the AI calls in a Node.js app without changes to your application code. You load it once, before your app starts. It records supported model requests, responses, token usage and timing as OpenTelemetry spans and sends them to Future AGI. OpenAI instrumentation is included, and recognized AI requests made through the global `fetch` are captured too. Other libraries are traced when you install their traceAI integration next to them.

The package is published on npm as `@traceai/auto` 0.4.1 (with `@traceai/fi-core` 1.4.1). It ships TypeScript type definitions and works with ES modules and CommonJS.

## Requirements

- Node.js 20.6 or later. On an older Node version the agent prints one warning and your app runs untraced.
- An existing Node application. Keep your model provider's credentials configured as usual; traceAI does not supply a model key.
- A [Future AGI](https://app.futureagi.com) API key and secret key.

## Install

Run this in your application's directory:

```sh
npm install @traceai/auto
```

To see which integrations your app could use, without installing anything:

```sh
npx --no-install traceai-auto bootstrap
```

Bootstrap lists the libraries it detects, their status, and suggested install commands. Add `--install` to install the missing integrations with npm, or `--install --package-manager=pnpm` (`yarn` is also supported). Bootstrap does not install your app's own AI SDKs. Restart the app after you install an integration.

## Turn it on

Preload the agent when you start Node:

```sh
node --import @traceai/auto/register app.mjs
```

Replace `app.mjs` with your entry file. For CommonJS, the same `--import` preload works, or use:

```sh
node --require @traceai/auto/register app.cjs
```

To keep your existing start command, for example an npm script, put the preload in `NODE_OPTIONS`:

```sh
NODE_OPTIONS="--require @traceai/auto/register" npm start
```

Use `--require` in `NODE_OPTIONS`. Every Node version accepts it, while a Node without `--import` refuses to start when `NODE_OPTIONS` contains it. Keep any other Node flags your deployment already needs.

Run from the application's directory so the target libraries and integrations can be found. Do not add a second preload or patch the same client by hand.

Set all configuration **before** the agent loads. Later `init()` calls do not change it.

### One line in code

If you prefer to start tracing from code, call `init()` from `@traceai/auto`. With ES modules, put this call in a separate file that you load with `--import`. A top-level `init()` next to static SDK imports can run after those SDKs have loaded.

```js
import { init } from "@traceai/auto";
init();
```

### Next.js

Use Next's `instrumentation.ts` and load the agent only in the Node.js runtime:

```ts
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("@traceai/auto/register");
  }
}
```

Add `@traceai/auto` and the AI SDKs you want traced (for example `openai`) to `serverExternalPackages` in `next.config.mjs`. Set the FI variables on the server, not as `NEXT_PUBLIC_*` variables. Bundlers (esbuild, Vite SSR, Rollup, webpack) and AWS Lambda have their own recipes in [Frameworks and deployment](../../typescript/packages/traceai_auto/docs/FRAMEWORKS.md).

## Send traces to Future AGI

Set both keys and a project name, then start the app with the preload:

```sh
FI_API_KEY="your-api-key" FI_SECRET_KEY="your-secret-key" \
FI_PROJECT_NAME="my-ai-app" \
node --import @traceai/auto/register app.mjs
```

Store the keys in your deployment's secret manager or shell environment. Make an AI request, read the response, then open **Observe → my-ai-app** in Future AGI.

| Variable | Default | What it does |
| --- | --- | --- |
| `FI_API_KEY` + `FI_SECRET_KEY` | Unset | The key pair for the Future AGI destination. Set both. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, then the profile's `project`, then the working directory's name | The project in Observe. Set it for stable routing. |
| `FI_BASE_URL` | `https://api.futureagi.com` | Collector base URL or full traces endpoint. |
| `FI_PROJECT_TYPE` | `observe` | Set `experiment` to opt in to experiment projects. |
| `FI_SAMPLE_RATE` | Unset | Sampling ratio for AI spans, from `0` to `1`. |
| `FI_CONFIG_FILE` | `futureagi.toml` | Config file, read from the working directory. |

Settings are read from code options first, then environment variables, then `futureagi.toml`, then the default. Future AGI receives only AI spans. Your own HTTP, database and custom spans stay with your own OpenTelemetry setup. To also send your app's own spans, set `FI_FORWARD_APP_SPANS=true`.

## Hide prompt and response content

Prompts and responses are captured by default. To hide them, set these before startup:

```sh
FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true FI_HIDE_ERROR_DETAILS=true \
node --import @traceai/auto/register app.mjs
```

| Variable | `init()` option (under `traceConfig`) | Hides |
| --- | --- | --- |
| `FI_HIDE_INPUTS` | `hideInputs` | Input values and input messages |
| `FI_HIDE_OUTPUTS` | `hideOutputs` | Output values and output messages |
| `FI_HIDE_INPUT_MESSAGES` | `hideInputMessages` | Input message attributes |
| `FI_HIDE_OUTPUT_MESSAGES` | `hideOutputMessages` | Output message attributes |
| `FI_HIDE_INPUT_TEXT` | `hideInputText` | Input text content |
| `FI_HIDE_OUTPUT_TEXT` | `hideOutputText` | Output text content |
| `FI_HIDE_INPUT_IMAGES` | `hideInputImages` | Image input |
| `FI_HIDE_EMBEDDING_VECTORS` | `hideEmbeddingVectors` | Embedding vectors |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | `hideLLMInvocationParameters` | Invocation parameters and tool schemas |
| `FI_HIDE_ERROR_DETAILS` | `hideErrorDetails` | Error messages and stacks; error status and type stay |
| `FI_PII_REDACTION` | `piiRedaction` | Applies supported PII patterns |

All of these default to `false`, except error details. When `FI_HIDE_ERROR_DETAILS` is unset, the input and output value, message and text settings, and the invocation-parameter setting, also hide error details. The image, vector and PII settings do not. PII pattern redaction is off by default and is not complete PII detection. Use the hide settings for sensitive content. These settings apply to the copy the agent sends to Future AGI; other exporters keep their own privacy settings.

## What gets traced

**Included in the package:**

- OpenAI SDK calls (`openai` versions 4 to 7).
- Recognized AI requests through the global `fetch`: OpenAI-compatible chat, Responses, embeddings and media endpoints; Anthropic messages; Gemini and Vertex content and embeddings; Cohere chat, embed and rerank; Bedrock converse and invoke; Ollama; and selected Deepgram, ElevenLabs, AssemblyAI and Cartesia endpoints.

**With the matching `@traceai/*` integration installed** (configuration names):

- Import hook: `anthropic`, `bedrock`, `beeai`, `cerebras`, `chromadb`, `cohere`, `cursor-sdk`, `deepseek`, `fireworks`, `google-adk`, `google-genai`, `groq`, `guardrails`, `huggingface`, `instructor`, `lancedb`, `livekit`, `llamaindex`, `mcp`, `milvus`, `mistral`, `mongodb`, `n8n`, `ollama`, `openai-agents`, `pgvector`, `pinecone`, `pipecat`, `portkey`, `qdrant`, `redis`, `sagemaker`, `strands`, `together`, `typesafe`, `vertexai`, `vllm`, `weaviate`, `xai`.
- Enable entry (automatic when the installed integration has an enable API): `genkit`, `vercel`. Genkit creates its spans on the global tracer provider. On the default route they stay with your own provider and `report()` shows `genkit` as `manual`. Set `FI_FORWARD_APP_SPANS=true` to send them to Future AGI.
- Manual (use the integration's own API): `claude-agent-sdk`, `codex-sdk`, `elevenlabs`, `google-genai-sdk`, `huggingface-transformers`, `mastra`, `openrouter`, `pi-coding-agent`, `vitest-evals`, `voltagent`.

`langchain` stays `pending`: the agent does not record LangChain spans. The model requests LangChain sends are still recorded, by the provider SDK's integration or as wire spans. Use `@traceai/langchain`'s own API for LangChain spans. Each integration keeps its own supported methods and versions. The full table, with target libraries and package names, is in the [package README](../../typescript/packages/traceai_auto/README.md#library-coverage).

Incoming HTTP spans are off by default. Database integrations that the agent turns on (pgvector, Redis, MongoDB) trace vector operations only.

## Turn integrations off or choose them

| Variable | Default | What it does |
| --- | --- | --- |
| `FI_INSTRUMENTATIONS` | All eligible integrations, except `http` | Allowlist, for example `openai,wire` |
| `FI_DISABLED_INSTRUMENTATIONS` | Empty | Denylist; it wins over the allowlist |
| `FI_WIRE_CAPTURE` | `true` | Set `false` to turn off global-fetch capture |
| `OTEL_SDK_DISABLED` | `false` | Set `true` to turn off the agent |

Lists are comma-separated and case-insensitive. Use the configuration names above, plus `wire` (global-fetch capture) and `http` (incoming HTTP spans). Incoming HTTP spans are on only when `FI_INSTRUMENTATIONS` names `http`, and they need `@opentelemetry/instrumentation-http` installed.

```sh
FI_DISABLED_INSTRUMENTATIONS=wire node --import @traceai/auto/register app.mjs
```

## Check that it works

Run the doctor from the app directory, with the same FI environment as the app:

```sh
npx --no-install traceai-auto doctor
```

When instrumentation is enabled, doctor sends one test span. It always prints a JSON report. Check the `collector` field: `accepted`, `failed` or `not_tested (...)`. The exit code alone does not tell you the collector is healthy. For the report without the test span:

```sh
npx --no-install traceai-auto doctor --no-check
```

Each integration reports one status: `instrumented`, `pending`, `not_installed`, `missing_instrumentor`, `disabled`, `manual` or `failed`. To print the startup report from your running app, set `FI_AUTO_DEBUG=true`. In code, `report()` from `@traceai/auto` returns the same information.

## Troubleshooting

- **Everything is `disabled`:** set both FI keys in the same place, and check `OTEL_SDK_DISABLED` and your allow and deny lists.
- **`collector: failed`:** check `FI_BASE_URL`, both keys, outbound connectivity and collector access.
- **`missing_instrumentor`:** install the package that bootstrap suggests, then restart.
- **A library stays `pending`:** load the agent before the SDK, check the supported versions, and keep the SDK external when you bundle.
- **No wire span:** read or cancel the response. Direct `undici.fetch`, `node-fetch`, WebSocket and gRPC are not global-fetch capture.
- **Duplicate spans:** remove extra preloads or manual instrumentation of the same client.

## Limits

- Preload every Node worker or child process you want to trace. Each one runs its own agent.
- The edge and browser runtimes are not supported. The preload is for Node only.
- Unread responses do not produce completed wire spans. Read or cancel each response.
- The default text body cap is 4 MiB (`FI_WIRE_MAX_BODY_BYTES`). Audio bytes are counted, not exported.
- Spans are flushed when Node exits on its own. Before an explicit exit, `await forceFlush()` or `await shutdown()` from `@traceai/auto`. On AWS Lambda, flush at each invocation; see the [Lambda recipe](../../typescript/packages/traceai_auto/docs/FRAMEWORKS.md#aws-lambda-and-short-lived-processes).
- Delivery is best effort. Hard termination, a frozen Lambda or collector failures can lose spans. Your app does not depend on the export succeeding.

## More detail

- [Package README](../../typescript/packages/traceai_auto/README.md): setup, full library table, privacy defaults and every limit.
- [Configuration reference](../../typescript/packages/traceai_auto/docs/CONFIGURATION.md): every environment variable, `init()` option, config file key and CLI command.
- [Frameworks and deployment](../../typescript/packages/traceai_auto/docs/FRAMEWORKS.md): Next.js, bundlers, AWS Lambda and edge runtimes.
- [Architecture](../../typescript/packages/traceai_auto/docs/ARCHITECTURE.md): capture behaviour and transport limits.
