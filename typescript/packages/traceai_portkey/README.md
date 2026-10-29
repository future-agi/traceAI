# @traceai/portkey

OpenTelemetry instrumentation for Portkey AI Gateway. Part of traceAI, Future AGI's OpenTelemetry instrumentation libraries.

[Documentation](https://docs.futureagi.com/docs/integrations/traceai/portkey) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_portkey) · [Issues](https://github.com/future-agi/traceAI/issues)

## Runtime support and prerequisites

`portkey-ai@1.10.4` creates `chat`, `completions`, and `embeddings` on
client instances. This instrumentor looks for those properties on
`Portkey.prototype`, so neither automatic registration nor passing the SDK module
to `manuallyInstrument()` captures that version's calls. The package version
range is not a compatibility guarantee.

Use the explicit application-span fallback below until instance instrumentation
is fixed. It captures operation duration/errors only, not automatic prompts,
responses, token usage, or cost. For streams, consume the stream inside the
callback; do not return an unconsumed stream.

## Install

The checked SDK version is `portkey-ai@1.10.4`. Install the SDK only after satisfying
its prerequisites above. The fallback requires these tracing dependencies:

```sh
npm install @traceai/fi-core @opentelemetry/api@^1.9.0 @opentelemetry/context-async-hooks@^2.0.1
```

`@traceai/portkey` is the automatic-instrumentation package, not a requirement for the
manual fallback. Its corrected source entry points resolve for both CommonJS and
ESM (through one CommonJS implementation), but still need a patch release.
Previously published `0.1.0` tarballs are unchanged. **Packaging repair does not
fix the provider runtime limitations above.**

## ESM application-span fallback

Save this as `tracing.mjs`. Set `FI_API_KEY` and `FI_SECRET_KEY` in the environment
for Future AGI export. Never embed credentials in source.

```javascript
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
// Use the core's CommonJS entry from ESM as well.
const { register, ProjectType } = require("@traceai/fi-core");
const { context, SpanStatusCode } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");

// Standalone process only. Reuse an existing OTel context manager if present.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
export const tracerProvider = register({
  projectName: "portkey-app",
  projectType: ProjectType.OBSERVE,
});
const tracer = tracerProvider.getTracer("portkey-application");

export async function traceSdkCall(operation) {
  return tracer.startActiveSpan("portkey.chat.completions", {
    attributes: { "fi.span.kind": "LLM" },
  }, async (span) => {
    try {
      const result = await operation();
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error) {
      // Do not record exception text: provider errors can contain sensitive data.
      span.setStatus({ code: SpanStatusCode.ERROR });
      throw error;
    } finally {
      span.end();
    }
  });
}
```

In your application's `.mjs` file, use an already configured SDK instance:

```javascript
import { traceSdkCall, tracerProvider } from "./tracing.mjs";
try {
  // client is an already configured Portkey client; use your own provider key.
  const result = await traceSdkCall(() => client.chat.completions.create({
    model: process.env.PORTKEY_MODEL,
    messages: [{ role: "user", content: "Hello" }],
    stream: false,
  }));
} finally {
  await tracerProvider.shutdown();
}
```

The fallback was checked with mocked asynchronous SDK operations, including a
nested child span and an error. This verifies span export and context continuity,
not a live model request or all SDK features. `register()` alone does not install
an async context manager; the explicit setup above is needed for nested traces.
Keep shutdown at process exit for long-running applications.

## Privacy

This fallback deliberately records no prompts or responses. Other instrumentors
and SDK telemetry may capture sensitive data; configure them separately before
sending production traffic.

## License

Apache-2.0.
