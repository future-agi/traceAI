# @traceai/strands

OpenTelemetry instrumentation for AWS Strands Agents SDK. Part of traceAI, Future AGI's OpenTelemetry instrumentation libraries.

[Documentation](https://docs.futureagi.com/docs/tracing) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_strands) · [Issues](https://github.com/future-agi/traceAI/issues)

## Runtime support and prerequisites

`@strands-agents/sdk@0.1.6` is ESM-only and requires **Node >=20**.
Node 18 and a CommonJS `require("@strands-agents/sdk")` setup are not supported
by this guide. Import the SDK in an ESM application and configure a model with
your own provider credentials before invoking the agent.

Automatic ESM patching is not verified: registering `StrandsInstrumentation`
does not by itself prove that native ESM imports are patched. Do not pass an
immutable ESM namespace to `manuallyInstrument()`, which mutates its argument.
Use the verified explicit application-span fallback below instead. It does not
claim automatic tool/LLM spans or token usage. Consume any streaming operation
inside the callback, before the span ends.

## Install

The checked SDK version is `@strands-agents/sdk@0.1.6`. Install the SDK only after satisfying
its prerequisites above. The fallback requires these tracing dependencies:

```sh
npm install @traceai/fi-core @opentelemetry/api@^1.9.0 @opentelemetry/context-async-hooks@^2.0.1
```

`@traceai/strands` is the automatic-instrumentation package, not a requirement for the
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
  projectName: "strands-app",
  projectType: ProjectType.OBSERVE,
});
const tracer = tracerProvider.getTracer("strands-application");

export async function traceSdkCall(operation) {
  return tracer.startActiveSpan("strands.invoke", {
    attributes: { "fi.span.kind": "AGENT" },
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
  // agent is an already configured Agent imported from @strands-agents/sdk.
  const result = await traceSdkCall(() => agent.invoke("Hello"));
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
