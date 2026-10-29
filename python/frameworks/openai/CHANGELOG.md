## [0.1.11] - 2026-10-29
### Fixed
- Embeddings respect `hide_inputs` for indexed input text and `hide_outputs` for both raw and indexed vectors, including with older fi-core releases.
- Chat streams used with `with` / `async with` retain the traced iterator and finish once on exhaustion or early context exit; context-body errors mark an unfinished span as ERROR.
- Streamed and failed Chat Completions spans now carry `gen_ai.request.model` (the requested model); a successful non-streamed response still records the served model.
- `gen_ai.provider.name` follows the client's `base_url` host for a small, explicit set of OpenAI-compatible vendors (Azure, Google, Vertex AI, DeepSeek, xAI, Mistral, Anthropic, Baseten, DashScope, Databricks, Lepton, Perplexity, OrcaRouter, Anannas, Doubleword). Unknown hosts, proxies and loopback servers keep `openai`. Previously the label was always `openai`, even for Azure and Google hosts.
- Responses API: non-streamed `output.value` is valid JSON with `output.mime_type=application/json` (it was Python `str(dict)`) and leads with `output_text`; a string `input` is no longer JSON-quoted; text-only assistant messages are a flat `message.content`, so trace views show the answer instead of `{}`.
- Embeddings: the span records the embedding model in `gen_ai.request.model`; `output.value` is a compact JSON summary (model, count, dimensions) instead of the vectors; `FI_HIDE_EMBEDDING_VECTORS` now also strips vectors from the raw `embedding.embeddings` JSON; items without `index` (e.g. Gemini's OpenAI-compatible API) are no longer skipped. Token attributes stay omitted when the provider returns no usage.
- `input.mime_type` / `output.mime_type` match the value actually recorded.

## [0.1.8] - 2025-07-08
### Feature
- Introduced compatibility with OpenAI's latest response method
- Enabled functionality for OpenAI's latest multi-modal feature

## [0.1.7] - 2025-06-10
### Feature
- Added support for ai-evaluation

## [0.1.6] - 2025-05-29
### Feature
- Updated dependencies to the latest versions.

## [0.1.5] - 2025-05-23
### Feature
- Added support for FutureAGI's protect

## [0.1.4] - 2025-05-08
### Feature
- Updated dependencies to the latest versions.

## [0.1.3] - 2025-04-14
### Changed
- Updated dependencies to the latest versions.
- Added Support for Audio and Image Generation models
