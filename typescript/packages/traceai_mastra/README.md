# TraceAI Mastra

Mastra v1 observability spans mapped to Future AGI's `gen_ai.span.kind`,
`input.value` and `output.value` conventions. Legacy v0 telemetry exports live at
`@traceai/mastra/legacy`; they are not the v1 API.

## Installation

```sh
npm install @traceai/mastra @mastra/observability @mastra/otel-exporter
```

The zero-code loader needs Node's synchronous `module.registerHooks` API (Node
22.15+ or 23.5+). Native CommonJS and ESM paths are tested with `@mastra/core`
1.37.1, `@mastra/observability` 1.14.0 and `@mastra/otel-exporter` 1.2.0.

## Zero-code bootstrap integration

With an `@traceai/auto` version that supports the Mastra enable entry, set
`FI_API_KEY` and `FI_SECRET_KEY`, then run the unmodified application:

```sh
node --import @traceai/auto/register ./app.js
# CommonJS: node -r @traceai/auto/register ./app.cjs
```

This package exposes a zero-argument `enableMastra(): boolean` at
`@traceai/mastra/auto` (also re-exported from the package root). It is an entry for
an agent bootstrap, **not** an environment-configuring replacement for
`@traceai/auto.init()`:

```ts
// Agent bootstrap code, not a change required in the user's Mastra application.
import { init } from "@traceai/auto";
import { enableMastra } from "@traceai/mastra/auto";

init(); // resolves FI_* configuration and installs the shared privacy exporter
const enabled = enableMastra(); // before loading the application's Mastra modules
```

`enableMastra` installs one process-wide source hook, with no startup network I/O.
It returns `false` rather than throwing when the agent/credentials or Node loader
API is unavailable. Repeated successful calls (including another package copy)
reuse the original installation; disabling/reconfiguring after enable is not
supported. The lean `/auto` entry does not import Mastra before the hook exists.
The root/manual entry records the observability classes it loads, so importing
`enableMastra` from the root before agent initialization is also supported in
CommonJS and ESM; enabling patches those cached context/factory boundaries.

- `new Mastra(...)` without observability gets a default observability instance.
- Application observability objects, named instances, selectors, sampling,
  processors, filters, bridges and existing exporters remain in place. Each
  supported instance receives one additional exporter named `future-agi`.
- Frozen plain configuration objects/exporter arrays are snapshotted, not mutated.
  Pre-instantiated observability instances keep their identity and receive the
  exporter through Mastra's public `registerExporter` method.
- An existing `future-agi` exporter is not duplicated. Instances added later via
  `Observability.registerInstance` are also handled.
- Mastra model execution supplies an OTel context carrier **inside** the preserved
  bridge's callback, so the wire layer skips duplicate model requests, including
  SDK-native descendants. If the bridge already selected the matching SDK span,
  that exact recording object stays active: application attributes, events and
  exceptions are not discarded. Tool execution does not inherit a model ancestor's
  coverage: a separate AI request made by a tool still receives its wire span.
  Invalid/NoOp Mastra spans leave upstream OTel context and wire fallback intact.
- Model coverage does not disable credential discovery. A header-only fetch
  observer discovers provider credentials after transport handoff, without reading
  bodies or changing promises/responses. Weak per-execution metadata forwards those
  secrets to the agent's existing sanitizer for converted and native spans,
  including errors delivered outside their original execution context. Unsupported
  header shapes/budget overflow fail closed at that same sanitizer.
- Converted `top_k` and `stop_sequences` invocation aliases are normalized into
  `gen_ai.request.parameters`, so the shared invocation-parameter hide flag applies
  without a separate privacy filter. They remain available there when not hidden.
- A native OTel bridge remains application-owned. When its synchronous span factory
  creates an identity observed by the agent's existing processor, that exact
  identity is not also converted/exported. Creation metadata follows the final
  export snapshot even when `spanOutputProcessors` return detached copies or a
  replacement `exportSpan` method; no completed-ID cache is needed. Other Mastra
  spans still use the converter; an arbitrary bridge name alone never disables
  forwarding.
- Conversion and enrichment receive detached, bounded data-only snapshots. Dates
  use intrinsic date methods, never application `toJSON`, getters, coercion or
  inspection hooks. Proxies and unsupported objects become `typeof` names;
  cycles/depth/node/text limits bound capture. Span and model timing retain their
  original precision. Workflow results and other exporters' snapshots are unchanged.
  The deprecated v0 exporter also snapshots SDK backing data before enrichment.
- `RAG_EMBEDDING` output is labelled as `{ "embeddings": <original output> }` in
  `output.value`, with no unlabelled vector alias. This lets the shared sanitizer
  apply both `hideEmbeddingVectors` / `FI_HIDE_EMBEDDING_VECTORS` and output hide
  controls, including anonymous numeric arrays and pre-serialized vector output.
  Capture remains on by default; application and other exporters' data is unchanged.
- Converted spans go to the initialized agent's **existing processor**, not a
  second collector transport. The shared export-time sanitizer, resolved project
  configuration, hide flags, sampling ratio, flush and shutdown apply. Mastra
  shutdown flushes but never shuts down that shared processor. Parent-based
  sampling decisions are captured at creation and retained through asynchronous
  delivery, including sampled upstream transactions at a low root ratio. A zero
  root ratio also keeps native SDK descendants unsampled.
- The agent-created default drops per-chunk `MODEL_CHUNK` spans. App-selected span
  exclusions are unchanged. Stream objects, helper methods and model errors are
  never wrapped by the exporter.

The `@traceai/auto` integration catalog/bootstrap must call this entry for the
launcher to activate it automatically. That cross-package wiring is outside
this package; merely installing this package does not modify the auto catalog.

## Manual Mastra v1 configuration

For an application not using the auto-agent bootstrap:

```ts
import { Mastra } from "@mastra/core";
import { createFIObservability } from "@traceai/mastra";

export const mastra = new Mastra({
  // agents, workflows, storage, ...
  observability: createFIObservability({
    serviceName: "weather-agent",
    projectName: "weather-agent",
    // FI_API_KEY / FI_SECRET_KEY / FI_BASE_URL are read from the environment.
  }),
});
```

The manual factories intentionally throw for missing credentials; the zero-code
entry fails open instead. To append manually, use `createFIMastraExporter()` in
an existing Mastra config's `exporters` array. Manual standalone exporters own
their OTLP batch processor; `observability.shutdown()` drains it.

## Tests

From the TypeScript workspace:

```sh
pnpm --filter @traceai/mastra test
```

The package script compiles the CommonJS runtime fixture and runs Jest in-band.
Tests use real Mastra packages and Mastra's mock model, in sequential fresh Node
processes, with no model credentials or external collector calls. They cover
CommonJS/ESM, the root and `/mastra` entrypoints, default/configured exporters,
frozen configs, instance identity, repeated enable, generate/stream baseline
parity, sample rate zero, throwing processors and the real auto-agent sanitizer
and wire deduplication boundary. The wire regressions explicitly enable both
`mastra` and `wire`, assert transport calls occurred, and cover workflow → agent →
tool parity, context-setting native bridges (duplicate IDs and coverage), root
imports before init, NoOp sampling fallback, low-ratio sampled parents, export
outside its upstream context, and zero-ratio native descendants. Regressions also
cover a root-cached constructor with frozen application config at low sampling,
recording SDK span object/attribute/event parity, real `@ai-sdk/openai` 3.0.124
mocked 401 errors (including native SDK exceptions), and hidden/visible `topK` and
`stopSequences` in both agent generation and direct real-Mastra model spans.
Credential regressions use the core S5 transport observer/process-wide registry,
not a Mastra fetch wrapper or per-trace credential inheritance. Real loopback
401 responses test `@ai-sdk/openai-compatible` `queryParams: { api_key: ... }`,
an already-started application transaction recording the provider error, and an
unrelated later transaction, in CommonJS/ESM against wire-only baselines. These
checks require redacted status/events/content with no duplicate wire span and
unchanged application errors and original application exporter snapshots. Mastra
registers only the explicit FI configuration credentials it handles outside the
transport; it does not inspect provider internals for transport credentials.
Data-only capture
regressions cover a real workflow returning the same Date with a counting/mutating
`toJSON` (agent on versus entirely off, CommonJS and ESM), model completion timing,
metadata/tags/error details, direct enrichment and invocation parameters, getters,
Proxy traps/revoked proxies, inspection/coercion hooks, cycles/budgets/non-finite
values, and legacy native SDK span/resource isolation. Replacement output processors
(including custom snapshot methods) are checked against real native-bridge baselines
in CommonJS/ESM. Real embedding spans exercise vector hiding, output hiding and
visible-by-default output for scalar vectors, vector matrices, structured outputs
and serialized strings. Legacy merged-resource precedence/routing is compared with
the native SDK, including nullish override fallbacks.

## Known limitations

- **2026-10-30:** Zero-code credential redaction relies on the core agent's S5
  transport hooks and process-wide export boundary, including for covered model
  requests and already-started application spans. Application-transformed secrets
  (for example splitting/base64) and unmarked credentials that never cross an
  instrumented transport are outside that guarantee. Standalone/manual exporters
  do not install those hooks. Use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and
  `FI_HIDE_ERROR_DETAILS` to suppress captured content.
- **2026-10-30:** Deprecated v0 manual export supports native SDK data backing
  slots, not custom getter/method-only span identities or lazy asynchronous
  resources. It does not invoke custom `spanContext`/`TraceState.serialize` methods;
  unsupported carriers are skipped and legacy tracestate is omitted.


- **2026-10-30:** Node 20 and Node versions without `module.registerHooks` do not
  support this loader; `enableMastra()` returns `false`. Standalone manual
  configuration remains available.
- **2026-10-30:** Enable before application Mastra imports. Observability classes
  loaded by this package's root/manual entry are handled, but arbitrary already
  evaluated application modules/created agents are not retroactively patched; bundled/inlined Mastra,
  remote modules and alternate package layouts are not covered. Unrecognized
  source layouts are left untouched rather than guessed at.
- **2026-10-30:** Runtime coverage is verified against the exact versions listed
  above, not every release allowed by the broader manual peer ranges. The hook
  recognizes Mastra's local `var Mastra/Observability/BaseSpan = class` bindings;
  upstream build changes require fresh compatibility tests.
- **2026-10-30:** Custom observability implementations without `getExporters` and
  `registerExporter` cannot accept automatic export. Explicitly empty/disabled
  observability stays empty; the agent does not override application opt-outs.
- **2026-10-30:** An existing application bridge is never replaced. Root-to-OTel
  transaction linking is available for supported default instances without a
  bridge, including plain configs created through a cached constructor. Custom
  observability instances keep their original root-linking behavior.
- **2026-10-30:** Automatic forwarding uses the auto-agent v1 shared-state and
  trusted-span snapshot contract. Independently installed incompatible agent
  versions are not supported. Export privacy guarantees apply to the agent path;
  standalone/manual exporters are not a substitute for its sanitizer.
- **2026-10-30:** Synthetic Mastra carriers preserve upstream IDs and sampling
  flags but do not propagate upstream W3C `tracestate`. Native SDK descendants
  retain the trace identity but can lose vendor correlation metadata. Reconfirmed
  in the 2026-10-30 independent verifier's `tracestate`/`tracestate-baseline`
  comparison; do not rely on this integration for end-to-end vendor `tracestate`
  propagation.
- **2026-10-30:** Mastra owns span timing and stream lifecycle. The exporter uses
  Mastra's original timestamps, never serialization time, but does not claim
  transport-byte timing precision or observe an unconsumed model stream's end.
