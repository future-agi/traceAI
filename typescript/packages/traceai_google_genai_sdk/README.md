# Google GenAI tracing for Node.js

Trace calls to the modern `@google/genai` SDK in Future AGI, including generation, chat, embeddings and Interactions.

Use Node.js 20 or later and `@google/genai` 1.33–2.x. This package does not instrument the older `@google/generative-ai` SDK; keep `@traceai/google-genai` for applications using that legacy SDK. Keep your Future AGI secret key on your server, not in a browser bundle.

## Install

```bash
npm install @google/genai@^2.32.0 @traceai/google-genai-sdk @traceai/fi-core
```

Set `GOOGLE_API_KEY`, `FI_API_KEY` and `FI_SECRET_KEY` in your server environment. Create your Future AGI keys in your workspace settings. The FI exporter sends OTLP traces to `https://api.futureagi.com/tracer/v1/traces`; it maps the FI environment variables to the `x-api-key` and `x-secret-key` collector headers. These credentials are not added to Google requests.

## Trace your client

Save this as `example.mjs` and run `node example.mjs`.

```javascript
import { GoogleGenAI } from '@google/genai';
import { register, ProjectType } from '@traceai/fi-core';
import { instrumentGoogleGenAI } from '@traceai/google-genai-sdk';

const provider = register({
  projectName: 'google-genai-app',
  projectType: ProjectType.OBSERVE,
  batch: true,
});
const ai = new GoogleGenAI({ apiKey: process.env.GOOGLE_API_KEY });
const tracing = instrumentGoogleGenAI(ai, { tracerProvider: provider });

try {
  const response = await ai.models.generateContent({
    model: 'gemini-2.5-flash',
    contents: 'Suggest a name for a neighborhood bookstore.',
  });
  console.log(response.text);

  for await (const chunk of await ai.models.generateContentStream({
    model: 'gemini-2.5-flash',
    contents: 'Write a one-sentence welcome for the bookstore.',
  })) {
    process.stdout.write(chunk.text ?? '');
  }
} finally {
  tracing.dispose();
  await provider.forceFlush();
  await provider.shutdown();
}
```

Open your `google-genai-app` project in Future AGI and inspect the generation spans. Each public SDK call is one logical span; automatic tool calling and continuation remain the SDK's responsibility, without additional synthetic tool-execution spans. Generation usage sums independent Google requests, counting each stream's cumulative usage only once. Tool definitions come from the declarations the SDK resolves, without invoking tools again. Existing OpenTelemetry parents and FI session/user context are retained.

If a generation request omits usage, `google.genai.usage_capture=partial` marks the observed counts as incomplete. If per-request observation is unavailable, for example because SDK methods were frozen before setup, usage is omitted and marked `google.genai.usage_capture=unavailable` rather than reporting the final tool hop as a complete call total.

## Supported calls

- `models.generateContent` and `generateContentStream`, including chat calls through `chats.create().sendMessage` and `sendMessageStream`.
- `models.embedContent`: text inputs, returned embedding count/dimensions and usage when exposed by the SDK. Vectors are never captured. Text-batch input counts are recorded only for unambiguous input shapes.
- `interactions.create`, `get`, `cancel` and `delete` when the installed SDK exposes them. Older SDK promise helpers are preserved: `withResponse()` retains parsed data and the HTTP response; `asResponse()` leaves the body unread and records request-only timing (`google.genai.response_capture=request_only`). Background calls describe submission or an explicit poll, not remote execution. No extra polling is performed.
- `models.generateImages`, `editImage` and `generateVideos` where the Google backend supports them. Video spans describe submission, not finished video generation. Media bytes and file URLs are never captured.

Consume streams with `for await`, or use the Interactions stream's reader/legacy readable conversion. Finish consumption or close the iterator when you stop early so its span can end. Stream branching/piping retains the SDK's behavior but ends a request-only span, marked `google.genai.stream_capture=request_only`; use iteration or readers for full stream timing and content. Live WebSocket sessions, cache/file/batch administration and token counting are not traced by this package; their SDK methods remain available.

## Privacy and lifecycle

The shared FI controls apply when you instrument the client: `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`, `FI_HIDE_EMBEDDING_VECTORS`, `FI_PII_REDACTION` and `FI_HIDE_ERROR_DETAILS`. Set them before calling `instrumentGoogleGenAI`, or pass the corresponding `traceConfig` options.

Text-hiding options omit that side's entire content surface, including tool arguments and schemas. Raw request/response objects, transport headers, media bytes, URLs, embedding vectors and provider error messages/stacks are never recorded, even with content capture enabled. Error spans retain error status and a generic error type. `FI_BASE64_IMAGE_MAX_LENGTH` cannot enable media capture. Content capture is bounded to 16,384 characters per call; adjust `maxContentLength` between 0 and 65,536. Truncated captures carry `google.genai.capture_truncated`.

`disable()` stops tracing new calls; `enable()` resumes it. `dispose()` stops tracing new calls immediately and restores the client's methods after in-flight calls drain. Repeated setup on a client returns the existing handle and retains its original options. To change providers or privacy settings, dispose and instrument again. Frozen methods cannot be instrumented; configure tracing before freezing your client. Always flush and shut down the provider when your process exits.

If you already configured an OpenTelemetry provider, pass it as `tracerProvider` instead of registering another provider. Instrument each client once, and avoid combining this package with another instrumentor for the same SDK calls.
