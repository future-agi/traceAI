## [0.1.17] - pending release
### Feature
- Embeddings now also set `gen_ai.request.model` from the request and `gen_ai.response.model` from the response when present, while retaining `embedding.model_name`.
- Streaming chat completions, completions and Responses record `gen_ai.response.time_to_first_token` (seconds from the request start to the first generated content: text, reasoning, refusals, tool calls or audio). Timing reads only the events the SDK already delivers and keeps no content.
- Chat completion responses are shared with `@traceai/fi-core` for the zero-code agent `@traceai/auto`.

## [0.1.14] - 2026-10-18
### Fix
- Content hiding now applies to the prompt. With `hideInputs` / `FI_HIDE_INPUTS=true`, `input.value`, `gen_ai.input.messages` and `raw.input` were still exported, because they are passed when the span is created and `@traceai/fi-core` did not mask creation-time attributes. Fixed in `@traceai/fi-core` 1.1.0; this release depends on it (0.1.13 was published against `@traceai/fi-core` `^0.1.13`, which has the bug).
- Error spans follow `hideErrorDetails` and get `error.type` (from `@traceai/fi-core` 1.1.0).

## [0.1.16](https://github.com/future-agi/traceAI/compare/npm-traceai-openai-v0.1.15...npm-traceai-openai-v0.1.16) (2026-11-05)


### Bug Fixes

* **fireworks:** trace TypeScript Fireworks clients through the shared OpenAI route ([#509](https://github.com/future-agi/traceAI/issues/509)) ([e746b14](https://github.com/future-agi/traceAI/commit/e746b143bfde8cf6d1c09df48d7a282318870ef0))
* **langchain:** protect retrieval outputs and close cancelled streams ([#561](https://github.com/future-agi/traceAI/issues/561)) ([8396fcc](https://github.com/future-agi/traceAI/commit/8396fcc3874186408615f44d8925c9cd9285cea4))
* **openai:** close privacy and SDK lifecycle release blockers ([#571](https://github.com/future-agi/traceAI/issues/571)) ([d7e5555](https://github.com/future-agi/traceAI/commit/d7e5555b60faa8b77d8f0db05942e91092fcfe94))
* **openai:** repair recalled TypeScript SDK contracts ([#535](https://github.com/future-agi/traceAI/issues/535)) ([6621397](https://github.com/future-agi/traceAI/commit/66213974be32daf79359ec092ab93a9913095ab1))

## [0.1.15](https://github.com/future-agi/traceAI/compare/npm-traceai-openai-v0.1.14...npm-traceai-openai-v0.1.15) (2026-11-01)


### Bug Fixes

* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))
* **openai:** attribute Vercel gateway TypeScript requests ([#463](https://github.com/future-agi/traceAI/issues/463)) ([0160ab6](https://github.com/future-agi/traceAI/commit/0160ab649bae3acf162b29fd30020410be65f25f))

## [0.1.10] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.9] - 2025-06-10
### Feature
- Bug Fixes

## [0.1.8] - 2025-06-10
### Feature
- Bug Fixes

## [0.1.7] - 2025-06-10
### Feature
- Bug Fixes

## [0.1.6] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.2] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.1] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.0] - 2025-06-10
### Feature
- Initial Release
- Added support for OpenAI
