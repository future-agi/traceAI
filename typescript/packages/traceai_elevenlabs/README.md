# @traceai/elevenlabs

Trace direct ElevenLabs speech synthesis and transcription calls in Future AGI, alongside the rest of your application's OpenTelemetry trace.

## Install

Requires Node.js 20.19 or later and `@elevenlabs/elevenlabs-js` 2.67–2.x.

```bash
npm install @traceai/elevenlabs @traceai/fi-core @elevenlabs/elevenlabs-js \
  @opentelemetry/api @opentelemetry/context-async-hooks
npm install --save-dev tsx typescript @types/node
```

## Configure credentials

```bash
export FI_API_KEY="<your-future-agi-api-key>"
export FI_SECRET_KEY="<your-future-agi-secret-key>"
export ELEVENLABS_API_KEY="<your-elevenlabs-api-key>"
export ELEVENLABS_VOICE_ID="<a-voice-id-from-your-elevenlabs-account>"
```

`@traceai/fi-core` sends the Future AGI credentials as `x-api-key` and `x-secret-key` headers to the OTLP traces endpoint. Do not put credentials in trace attributes.

## Generate and trace speech

Save this as `speech.ts`. The example writes the generated audio to `speech.mp3`; it does not upload audio to Future AGI.

```typescript
import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { register, ProjectType } from "@traceai/fi-core";
import { ElevenLabsClient } from "@elevenlabs/elevenlabs-js";
import { ElevenLabsInstrumentation } from "@traceai/elevenlabs";

const contextManager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(contextManager);
const provider = register({
  projectName: "speech-app",
  projectType: ProjectType.OBSERVE,
  endpoint:
    process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT ??
    "https://api.futureagi.com/tracer/v1/traces",
});
const instrumentation = new ElevenLabsInstrumentation({
  tracerProvider: provider,
});
const client = instrumentation.wrapClient(
  new ElevenLabsClient({
    apiKey: process.env.ELEVENLABS_API_KEY,
    baseUrl: process.env.ELEVENLABS_BASE_URL,
  }),
);

async function main() {
  try {
    await provider
      .getTracer("speech-app")
      .startActiveSpan("Create greeting", async (parent) => {
        try {
          const audio = await client.textToSpeech.convert(
            process.env.ELEVENLABS_VOICE_ID!,
            {
              text: "Welcome! Your appointment is confirmed.",
              modelId: "eleven_multilingual_v2",
              outputFormat: "mp3_44100_128",
            },
          );
          await pipeline(audio, createWriteStream("speech.mp3"));
        } finally {
          parent.end();
        }
      });
  } finally {
    instrumentation.shutdown();
    await provider.shutdown();
    context.disable();
    contextManager.disable();
  }
}
void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
```

```bash
npx tsx speech.ts
```

Open **Observe** in Future AGI, select the `speech-app` project, and open `Create greeting`. Its child span, `ElevenLabs textToSpeech.convert`, contains the provider, requested model, voice ID, text, first-audio latency in milliseconds, consumed byte count and completion classification.

If you already initialize OpenTelemetry, reuse your tracer provider and context manager instead of registering another global provider. Pass your provider to `new ElevenLabsInstrumentation({ tracerProvider })` and wrap your existing client. Explicit wrapping works with ESM and CommonJS, including clients imported before the instrumentation.

The optional `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and `ELEVENLABS_BASE_URL` overrides in the example allow a custom collector or SDK endpoint; leave them unset for the standard hosted services.

## Supported calls

| SDK method | Span ends when |
| --- | --- |
| `textToSpeech.convert` | You consume or cancel the audio stream |
| `textToSpeech.stream` | You consume or cancel the audio stream |
| `textToSpeech.convertWithTimestamps` | The complete response arrives |
| `textToSpeech.streamWithTimestamps` | You finish or stop iterating timestamp chunks |
| `speechToText.convert` | Synchronous transcription completes, including multi-channel output |

Use these methods on the wrapped client as usual. `withRawResponse()`, `then`, `catch`, `finally`, byte-stream readers (including BYOB when the upstream stream supports it), iteration and piping remain available. Timestamp responses retain audio and alignment data in your application, but that data is not exported. Requests that set `webhook: true`, realtime/WebSocket APIs and other SDK namespaces pass through without integration spans.

Hosted ElevenLabs Agents conversations are a separate integration. This package does not export conversation webhooks, audio attachments, usage tokens or estimated costs.

## Privacy

By default, the package follows core traceAI content settings: it captures up to 4,096 characters of input text and up to 4,096 characters of transcription output. Multi-channel transcripts share that output limit. To exclude text, set these before creating the instrumentation:

```bash
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_ERROR_DETAILS=true
```

`FI_HIDE_INPUT_MESSAGES` / `FI_HIDE_INPUT_TEXT` and their output equivalents also hide the corresponding speech text. You can pass core `traceConfig` options to the instrumentation instead. These settings affect traces, not SDK request or response values.

The package never exports raw audio, base64, alignment text, upload paths, source URLs, request headers or provider error bodies. Errors record an error status, a fixed error type, and an HTTP status when available, not exception messages or stack traces—even if `FI_HIDE_ERROR_DETAILS` is false. Your application is responsible for content it adds to parent spans and context metadata.

## Stream lifecycle

Tracing never drains audio for you. First-audio latency includes time until your application reads the first audio chunk; for `convertWithTimestamps`, it measures arrival of the complete response. One operation span covers SDK retries.

`elevenlabs.completion` is `complete`, `cancelled`, `error` or `incomplete`. A span has an absolute five-minute lifetime by default. Unread or unfinished operations then close as `incomplete`, without cancelling the SDK request or stream or inventing success. Late consumption does not reopen the span.

Adjust `maxSpanDurationMs` (1–86,400,000 ms) for long operations. `maxActiveSpans` defaults to 1,024; excess calls continue normally without tracing. Only constant-sized counters are retained per active operation, not audio buffers. `closePendingSpans()` closes outstanding telemetry as incomplete; `shutdown()` also disables the instrumentation. Call it before your provider's `shutdown()` to flush unfinished spans, as in the example.

For module-wide registration, call `instrumentation.manuallyInstrument(sdk)` with `import * as sdk from "@elevenlabs/elevenlabs-js"`. This patches namespace getters rather than installing an import hook. `disable()`/`unpatch()` restores those getters and makes existing client proxies pass through; register the module again after re-enabling it. Repeated wrapping or module registration does not add duplicate spans. The first enabled instrumentation owns a shared client until disabled.

## Troubleshooting

- **No span after receiving audio:** consume or cancel the returned stream. Receiving headers is not successful completion. Use `shutdown()` before exiting a short script.
- **Spans appear separately from your application trace:** initialize an async context manager once and call the wrapped client within the active application span.
- **Text is missing:** check the `FI_HIDE_*` variables and the instrumentation's `traceConfig`. Audio and provider error text are intentionally never captured.
- **Transcription spans are missing:** `webhook: true` submits an asynchronous job; this package does not trace its later completion.
