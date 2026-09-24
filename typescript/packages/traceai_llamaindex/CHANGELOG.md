## [0.2.0] - 2026-09-24
### Fix
- Trace LLM calls on current LlamaIndex, where OpenAI and other LLMs live in provider packages such as `@llamaindex/openai`: `manuallyInstrument(LlamaIndex, LlamaIndexOpenAI)` now instruments them, and auto-instrumentation patches `@llamaindex/openai` too.
- Auto-instrumentation no longer crashes the app when a hooked module is missing expected classes (e.g. under ESM); it logs a diag warning instead.
- Record tool calls on LLM spans, including every parallel tool call in a streamed response, and record the exception on errored spans.
- Record token counts on LLM spans when the provider returns usage; `@llamaindex/openai` does not request usage on streams, so streamed spans usually carry none.
- Record each input message with its own role; 0.1.1 overwrote the first message's role with `assistant`.
- `unpatch` now restores every method `patch` wraps, and patching the same module twice no longer double-wraps.
- Name spans after the runtime class, e.g. `OpenAIEmbedding.getQueryEmbedding` instead of `BaseEmbedding.getQueryEmbedding`, and a subclass of `OpenAI` gets its own name on chat spans.
- Use the `gen_ai.*` semantic conventions for LLM attributes.

## [0.1.0] - 2025-08-27
### Feature
- Added support for LlamaIndex Typescript instrumentation
