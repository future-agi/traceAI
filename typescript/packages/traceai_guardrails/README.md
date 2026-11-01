# @traceai/guardrails

Trace JavaScript Guardrails validation calls in Future AGI, including the actual pass/fail outcome and a `GUARDRAIL` span for each call.

## Install

`@guardrails-ai/core` 0.1.x is an ESM-only bridge to Python `guardrails-ai==0.4.2`. Use a separate Python environment for this bridge, not the environment of a newer Python Guardrails application. The example uses Node.js 22 and Python 3.10.

```sh
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install guardrails-ai==0.4.2
npm install @guardrails-ai/core@0.1.1 @traceai/guardrails @traceai/fi-core@^1.1.0 @opentelemetry/api@^1.9.0 @opentelemetry/context-async-hooks@^2.0.1
```

Keep that environment active when running Node, or set `PYTHON_BIN` to its Python executable. The SDK's npm postinstall also installs Python Guardrails; the explicit pip command covers npm installations that disable dependency scripts.

## Trace validation

Set `FI_API_KEY` and `FI_SECRET_KEY` using your Future AGI project credentials. Save the following as `guardrails.mjs`, then run `node guardrails.mjs`. No model-provider key is needed for this schema-validation example.

```javascript
import { createRequire } from "node:module";
import * as guardrails from "@guardrails-ai/core";

const require = createRequire(import.meta.url);
const { register, ProjectType } = require("@traceai/fi-core");
const { GuardrailsInstrumentation } = require("@traceai/guardrails");
const { context } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");

// In an existing OTel application, reuse its context manager and provider.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const provider = register({
  projectName: "guardrails-app",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new GuardrailsInstrumentation({
  traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true },
});
instrumentation.setTracerProvider(provider);
// Explicit patching works with the SDK's ESM namespace; no ESM loader is needed.
instrumentation.manuallyInstrument(guardrails);

try {
  const guard = await guardrails.Guard.fromRailString(
    '<rail version="0.1"><output><integer name="count" /></output></rail>'
  );
  const accepted = await guard.parse('{"count":7}');
  const rejected = await guard.parse('{"count":"not-an-integer"}');
  console.log(accepted.validationPassed); // true
  console.log(rejected.validationPassed); // false
} finally {
  instrumentation.disable();
  guardrails.exit();
  await provider.shutdown();
}
```

Open the **guardrails-app** project in Future AGI. Each `parse` call has a `GUARDRAIL` span with `guardrails.validation_passed` set to the SDK's `validationPassed` boolean. A rejected value is a normal validation result; an exception marks the span as an error and is rethrown unchanged. If an outcome is unavailable, the boolean attribute is omitted rather than reporting success.

For a long-running service, keep the instrumentation and Python bridge alive and shut them down only at process exit. `disable()` restores methods patched by this instance; `enable()` reapplies its manual patches.

## Scope and privacy

This package traces the JavaScript call boundary. It does not instrument individual validators or model calls inside the Python subprocess. For a Python application, use the [Python Guardrails integration](https://docs.futureagi.com/docs/integrations/traceai/guardrails) instead.

The example hides input, output, and exception details while retaining validation outcomes. Remove the hide options only when you intend to capture validation content. These settings do not configure other instrumentors or Python Guardrails' own Hub telemetry. To opt out of the bridge's Python telemetry, set `no_metrics=true` in its `.guardrailsrc` configuration, preserving any other existing settings.

## Troubleshooting

- **Python cannot import Guardrails:** reactivate the Python environment used during installation, or set `PYTHON_BIN` to its Python executable.
- **No JavaScript spans:** call `manuallyInstrument(guardrails)` before `parse`. Import order and `registerInstrumentations()` alone do not configure this ESM bridge.
- **Installing another Python Guardrails version changes behavior:** use separate virtual environments; this JavaScript SDK depends on Python Guardrails 0.4.2.

## License

Apache-2.0.
