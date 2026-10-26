## [0.2.0] - 2026-10-22
### Fix
- Support `@openai/agents` up to 0.21. The peer range is now `>=0.4.0 <0.22.0`. It was `^0.4.0`, so `npm install @traceai/openai-agents @openai/agents@latest` failed with `ERESOLVE`. The require hook now matches the same range.
- Read span data in the shape the SDK emits it, which is snake_case in every release from 0.4 onwards. Until now these fields were never captured:
  - handoffs (`from_agent` / `to_agent`): handoff spans were named `handoff` and `graph.node.parent_id` was never set
  - model settings (`model_config`)
  - token usage (`input_tokens` / `output_tokens`)
  - response spans (`_input` / `_response`): they had no input or output, so neither did the root span
- Response spans now record the model and token counts. Generation spans record the output text, and read token counts from the Chat Completions result when the SDK does not set `usage`.
- The ESM entry point (`exports["."].import` and `module`) pointed at `dist/esm/index.js`, but the build wrote `dist/esm/src/index.js` with extensionless imports, so `import "@traceai/openai-agents"` failed. The ESM and ESNext builds now use `rootDir: "src"` and the shared ESM base config, like the other packages.
- `manuallyInstrument()` no longer throws `TypeError` when it is given an ES module namespace (`import * as agents from "@openai/agents"`).
- `traceConfig` and `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` had no effect. The tracer was built inside the base-class constructor, before the config was stored, and was given `undefined`, which makes fi-core skip the environment variables. `input.value` and `output.value` are now redacted when hiding is on.
- A tracer provider passed with `setTracerProvider()` after construction was ignored unless `enable()` was called again, so spans went to the global provider. The tracer is now rebuilt when the module is patched.

### Feature
- New `exclusiveProcessor` option, default `true`. Set it to `false` to keep the SDK's default OpenAI exporter: the processor is then added with `addTraceProcessor` instead of replacing the existing processors with `setTraceProcessors`. This matches `exclusive_processor` in the Python package.
- Export `SUPPORTED_AGENTS_VERSIONS`.

### Tests
- New `realSdk.test.ts`. It runs a two-agent workflow (tool call, handoff, answer) through the real SDK against an in-process fake OpenAI server, for both the Responses API and Chat Completions, and checks the resulting span tree and the flush on shutdown. It also covers `traceConfig` and `FI_HIDE_*` redaction and a provider set with `setTracerProvider()` after construction. Run against `@openai/agents` 0.4.15, 0.11.8, 0.16.1 and 0.21.0.

## [0.1.0]
- Initial release.
