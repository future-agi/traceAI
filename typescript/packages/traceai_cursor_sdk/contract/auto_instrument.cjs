/**
 * Auto-instrumentation check (no network): `registerInstrumentations()` then
 * `require("@cursor/sdk")` patches the CJS build's Agent class through the
 * OpenTelemetry require hook.
 *
 *   node contract/auto_instrument.cjs
 */
const assert = require("node:assert/strict");
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { CursorSDKInstrumentation, isPatched } = require("../dist/src/index.js");

registerInstrumentations({ instrumentations: [new CursorSDKInstrumentation()] });
const { Agent } = require("@cursor/sdk");
assert.equal(isPatched(Agent), true, "require('@cursor/sdk') was not patched");
console.log("PASS auto-instrumentation patched require('@cursor/sdk') Agent");
