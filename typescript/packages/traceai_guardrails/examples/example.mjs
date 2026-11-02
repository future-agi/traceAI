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
