# Changelog

## [0.1.4] - pending release
### Feature
- `enableVercelInstrumentation()` (also exported as `enable`) and `VercelAIInstrumentation` trace the AI SDK's `generateText`, `streamText`, `generateObject`, `streamObject`, `embed` and `embedMany` without `experimental_telemetry`. The zero-code agent `@traceai/auto` uses it.

### Fix
- The FI privacy environment flags (`FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, their message/text/image variants, `FI_HIDE_EMBEDDING_VECTORS` and `FI_HIDE_ERROR_DETAILS`) now apply to exported AI SDK spans. A serialized payload is dropped whole when a content flag applies to it.
- `FISimpleSpanProcessor` and `FIBatchSpanProcessor` always omit `ai.request.headers.*` attributes from exported spans, events and links. No flag is required.
- The processors now export a separate copy. FI mapping and content hiding no longer change the span seen by other processors.
- `addFIAttributesToSpan` keeps existing attribute values, so native GenAI attributes take precedence over mapped values.
- AI SDK 7 spans with supported `gen_ai.operation.name` values now receive FI span kinds when `operation.name` is absent. `ai.rerank` and `ai.rerank.doRerank` map to `RERANKER`.
- Tool-message conversion reads AI SDK 5+ `input` and `output` fields, with `args` and `result` fallbacks. It emits one message per tool result and preserves available tool-call IDs in output `tool_calls`.
- When `FI_HIDE_ERROR_DETAILS` is unset or invalid, input/output, message or text hiding flags also hide error details. `FI_HIDE_INPUT_IMAGES` alone does not trigger this. Explicit `FI_HIDE_ERROR_DETAILS=true` or `false` takes precedence.

### Runtime
- The package root is now Node.js-only. Importing it also loads auto-instrumentation and `@traceai/fi-core`, even when using only the manual processors.
- Use OpenTelemetry SDK 1.x providers and exporters with `FISimpleSpanProcessor` and `FIBatchSpanProcessor`. These processors do not support SDK 2.x.

### Dependencies
- Adds `@traceai/fi-core` ^1.2.0 and `@opentelemetry/instrumentation` ^0.202.0. `@opentelemetry/sdk-trace-base` is now ^1.30.1 (was >=1.19.0 <2.0.0).

## [0.1.3](https://github.com/future-agi/traceAI/compare/npm-traceai-vercel-v0.1.2...npm-traceai-vercel-v0.1.3) (2026-11-05)


### Bug Fixes

* **ci:** release already-merged SDK changes at bootstrap ([0a13942](https://github.com/future-agi/traceAI/commit/0a139428d589d3d5a2ebda0a250f1928efe4b9b8))
* **npm:** declare the runtime dependencies of @traceai/vercel and @traceai/bedrock ([1ead4ec](https://github.com/future-agi/traceAI/commit/1ead4ec15d15365cabea32d15085b5c48404d99b))
* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))
* **release:** cover core manifests and stage npm dependencies ([896d14e](https://github.com/future-agi/traceAI/commit/896d14eb530ea38d449f8a4b14b523530b17d2e2))
* **release:** unblock the first main release found by a dry run ([4965783](https://github.com/future-agi/traceAI/commit/49657838882374f3b19df5d6a874140aff28d784))

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-vercel-v0.1.1...npm-traceai-vercel-v0.1.2) (2026-11-01)


### Bug Fixes

* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to npm; one-time bootstrap patch for the automated release pipeline.
- Declare `@opentelemetry/sdk-trace-base` as a dependency. The span processor imports it at runtime, so a clean install without it failed with "Cannot find module".
