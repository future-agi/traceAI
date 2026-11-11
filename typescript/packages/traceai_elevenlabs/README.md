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

Use these methods on the wrapped client as usual. `withRawResponse()`, `then`, `catch`, `finally`, byte-stream readers (including BYOB when the upstream stream supports it), iteration and piping remain available. Timestamp responses retain audio and alignment data in your application. By default, neither is exported. Requests that set `webhook: true`, realtime/WebSocket APIs and other SDK namespaces pass through without integration spans.

Hosted ElevenLabs Agents conversations are a separate integration. This package does not export conversation webhooks, usage tokens or estimated costs.

## Optional binary attachments

Binary capture is off by default. If your destination supports the attachment service, explicitly enable your provider's attachment manager and pass that **same manager** as `attachments: provider.attachments` to `ElevenLabsInstrumentation`. Await `provider.attachments.ready()` at startup to avoid missing early calls; continue to use the provider's `shutdown()` for delivery. The adapter never creates another uploader or enables recording.

- `textToSpeech.convertWithTimestamps` can attach a complete MP3 response up to 256 KiB. The output format must be the SDK default or a supported MP3 format. Generated audio is not proof of playback.
- `speechToText.convert` can attach an existing `Blob`/`File` when you also set `attachmentInputMimeType` to its exact MIME type: `audio/wav`, `audio/mpeg`, `audio/ogg`, `audio/mp4`, `audio/webm` or `audio/flac`. Filenames are not exported; mismatched or missing types are not sniffed.
- `textToSpeech.convert`, `textToSpeech.stream` and `textToSpeech.streamWithTimestamps` can attach the MP3 your application **reads to the end**, up to 4 MiB per call (about 4 minutes at the default 128 kbit/s). Tracing copies only the chunks you already read; it never reads ahead, tees or drains the stream. If you cancel, stop iterating, hit an error or exceed 4 MiB, nothing is uploaded: a partial file is never stored as if complete. While a stream is being read, the manager reserves the full 4 MiB, so many concurrent long streams can report `queue_full` and pass through uncaptured.
- PCM/ulaw output, local paths and remote URLs are not captured. Alignment data stays in your SDK response, not in attachment metadata.

Attachments obey the manager's default-off, server, sampling and PII policies in addition to the instrumentation's directional hide settings. Without a qualified speech sanitizer, `piiRedaction` denies capture. Existing raw audio may contain embedded metadata; a policy requiring metadata removal also denies it. The span's `elevenlabs.attachment.input` / `elevenlabs.attachment.output` value is the admission result; `queued` means admitted for transfer, **not** validated or playable. Content is out of band, never base64 in OTLP. A call made through this bound instrumentation carries the shared media-owner context marker (`traceai.auto.attachment_owner.v1`), so wire-level capture that honours the marker does not upload the same audio again.

## Privacy

By default, the package follows core traceAI content settings: it captures up to 4,096 characters of input text and up to 4,096 characters of transcription output. Multi-channel transcripts share that output limit. To exclude text, set these before creating the instrumentation:

```bash
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_ERROR_DETAILS=true
```

`FI_HIDE_INPUT_MESSAGES` / `FI_HIDE_INPUT_TEXT` and their output equivalents also hide the corresponding speech text. You can pass core `traceConfig` options to the instrumentation instead. These settings affect traces, not SDK request or response values.

Trace attributes never contain raw audio, base64, alignment text, upload paths, source URLs, request headers or provider error bodies. Binary audio leaves the process only through the optional attachment manager described above. Errors record an error status, a fixed error type, and an HTTP status when available, not exception messages or stack traces—even if `FI_HIDE_ERROR_DETAILS` is false. Your application is responsible for content it adds to parent spans and context metadata.

## Stream lifecycle

Tracing never drains audio for you. First-audio latency includes time until your application reads the first audio chunk; for `convertWithTimestamps`, it measures arrival of the complete response. One operation span covers SDK retries.

`elevenlabs.completion` is `complete`, `cancelled`, `error` or `incomplete`. A span has an absolute five-minute lifetime by default. Unread or unfinished operations then close as `incomplete`, without cancelling the SDK request or stream or inventing success. Late consumption does not reopen the span.

Adjust `maxSpanDurationMs` (1–86,400,000 ms) for long operations. `maxActiveSpans` defaults to 1,024; excess calls continue normally without tracing. Only constant-sized counters are retained per active operation, not audio buffers. `closePendingSpans()` closes outstanding telemetry as incomplete; `shutdown()` also disables the instrumentation. Call it before your provider's `shutdown()` to flush unfinished spans, as in the example.

For module-wide registration, call `instrumentation.manuallyInstrument(sdk)` with `import * as sdk from "@elevenlabs/elevenlabs-js"`. This patches namespace getters rather than installing an import hook. `disable()`/`unpatch()` restores those getters and makes existing client proxies pass through; register the module again after re-enabling it. Repeated wrapping or module registration does not add duplicate spans. The first enabled instrumentation owns a shared client until disabled.

## Troubleshooting

- **No span after receiving audio:** consume or cancel the returned stream. Receiving headers is not successful completion. Use `shutdown()` before exiting a short script.
- **Spans appear separately from your application trace:** initialize an async context manager once and call the wrapped client within the active application span.
- **Text is missing:** check the `FI_HIDE_*` variables and the instrumentation's `traceConfig`. Binary audio needs the separate attachment opt-in; provider error text is never captured.
- **Transcription spans are missing:** `webhook: true` submits an asynchronous job; this package does not trace its later completion.
