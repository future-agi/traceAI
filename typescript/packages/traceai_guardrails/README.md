# @traceai/guardrails

OpenTelemetry instrumentation for Guardrails AI. Part of traceAI, Future AGI's OpenTelemetry instrumentation libraries.

[Documentation](https://docs.futureagi.com/docs/integrations/traceai/guardrails) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_guardrails) · [Issues](https://github.com/future-agi/traceAI/issues)

## Runtime support and prerequisites

`@guardrails-ai/core@0.1.1` is ESM-only and requires Node >=18. Its
postinstall runs `python3 -m pip install guardrails-ai==0.4.2`. You need a working
`python3` and `pip`, network access to PyPI, and a writable **isolated Python
virtual environment** active during npm installation and runtime. Skipping that
postinstall does not produce a usable provider installation.

A CommonJS `require("@guardrails-ai/core")` setup is not supported by this guide.
Automatic ESM patching is not verified: `registerInstrumentations()` and import
order alone are not sufficient. Do not pass an immutable ESM module namespace to
`manuallyInstrument()`, which mutates its argument. The SDK's Python installation
and real validation operations have not been executed in this metadata check.

The verified ESM fallback below creates an explicit application span around your
own awaited validation operation. It does not depend on SDK patching and does not
claim automatic validator or LLM tracing.

## Install

The checked SDK version is `@guardrails-ai/core@0.1.1`. Install the SDK only after satisfying
its prerequisites above. The fallback requires these tracing dependencies:

```sh
npm install @traceai/fi-core @opentelemetry/api@^1.9.0 @opentelemetry/context-async-hooks@^2.0.1
```

`@traceai/guardrails` is the automatic-instrumentation package, not a requirement for the
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
  projectName: "guardrails-app",
  projectType: ProjectType.OBSERVE,
});
const tracer = tracerProvider.getTracer("guardrails-application");

export async function traceSdkCall(operation) {
  return tracer.startActiveSpan("guardrails.parse", {
    attributes: { "fi.span.kind": "CHAIN" },
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
  // guard is an already configured Guard imported in your ESM application.
  // Configure its validators and Python environment before invoking it.
  const result = await traceSdkCall(() => guard.parse("candidate output"));
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
