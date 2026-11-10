## [Unreleased]
### Fixed
- Add Azure client deployment, API version and endpoint host/explicit port span metadata through the existing OpenAI instrumentor, without recording full URLs or credentials (#154).
- Normally exhausted Chat Completions streams now export ERROR when at least one
  choice reports a non-null `finish_reason` but a requested choice index never does.
  This honors the effective on-wire `n`, including `extra_body` overrides, for sync
  and async streams on the shared OpenAI/DeepSeek route. All non-null finish reasons
  count as terminal, including `length` and `content_filter`. Caller-initiated early
  close/context exit remains OK unless an exception occurs. Known limitation:
  streams with no `finish_reason` at all are not judged and remain OK.

## [0.1.14] - 2026-11-10
### Fixed
- Attribute OpenAI-compatible requests to `api.cometapi.com` to `cometapi` (#348), including streamed and failed requests.
- Attribute OpenAI-compatible requests to `api.novita.ai` to `novita` (#397), including streamed and failed requests.

## [0.1.11] - 2026-10-29
### Fixed
- Attribute OpenAI-compatible calls to `api.together.ai` to `together`, including streamed and failed requests; unknown proxies and look-alike hosts still use `openai`.
- Embeddings respect `hide_inputs` for indexed input text and `hide_outputs` for both raw and indexed vectors, including with older fi-core releases.
- Chat streams used with `with` / `async with` retain the traced iterator and finish once on exhaustion or early context exit; context-body errors mark an unfinished span as ERROR.
- Streamed and failed Chat Completions spans now carry `gen_ai.request.model` (the requested model); a successful non-streamed response still records the served model.
- `gen_ai.provider.name` follows the client's `base_url` host for a small, explicit set of OpenAI-compatible vendors (Azure, Google, Vertex AI, DeepSeek, xAI, Mistral, Anthropic, Baseten, DashScope, Databricks, Lepton, Perplexity, OrcaRouter, Anannas, Doubleword). Unknown hosts, proxies and loopback servers keep `openai`. Previously the label was always `openai`, even for Azure and Google hosts.
- Responses API: non-streamed `output.value` is valid JSON with `output.mime_type=application/json` (it was Python `str(dict)`) and leads with `output_text`; a string `input` is no longer JSON-quoted; text-only assistant messages are a flat `message.content`, so trace views show the answer instead of `{}`.
- Embeddings: the span records the embedding model in `gen_ai.request.model`; `output.value` is a compact JSON summary (model, count, dimensions) instead of the vectors; `FI_HIDE_EMBEDDING_VECTORS` now also strips vectors from the raw `embedding.embeddings` JSON; items without `index` (e.g. Gemini's OpenAI-compatible API) are no longer skipped. Token attributes stay omitted when the provider returns no usage.
- `input.mime_type` / `output.mime_type` match the value actually recorded.

## [0.1.13](https://github.com/future-agi/traceAI/compare/traceai-openai-v0.1.12...traceai-openai-v0.1.13) (2026-11-05)


### Bug Fixes

* **deepseek:** migrate to the shared OpenAI tracing route ([#376](https://github.com/future-agi/traceAI/issues/376)) ([9864092](https://github.com/future-agi/traceAI/commit/986409265a6270ae3821b6988f5af12af96bb505))
* **fireworks:** prefer a single OpenAI-compatible trace ([#367](https://github.com/future-agi/traceAI/issues/367)) ([84aa857](https://github.com/future-agi/traceAI/commit/84aa857c90be0eb0aebce7bc31137dd6ddbeeee6))
* **openai:** close privacy and SDK lifecycle release blockers ([#571](https://github.com/future-agi/traceAI/issues/571)) ([d7e5555](https://github.com/future-agi/traceAI/commit/d7e5555b60faa8b77d8f0db05942e91092fcfe94))
* **openai:** migrate vLLM and attribute Ollama clients ([#424](https://github.com/future-agi/traceAI/issues/424)) ([cd4ea17](https://github.com/future-agi/traceAI/commit/cd4ea177ac4c5426a9df7aeff870154656f71763))
* **parallel:** cover Task workflows and Responses attribution ([#486](https://github.com/future-agi/traceAI/issues/486)) ([c10a016](https://github.com/future-agi/traceAI/commit/c10a0165d2b58c827844d3617c20bb19e832ed17))
* **parallel:** protect Responses privacy and interrupted workflows ([#562](https://github.com/future-agi/traceAI/issues/562)) ([b17c618](https://github.com/future-agi/traceAI/commit/b17c618a5cc910628e91e45a2cb08acc987c0b5d))

## [0.1.12](https://github.com/future-agi/traceAI/compare/traceai-openai-v0.1.11...traceai-openai-v0.1.12) (2026-11-01)


### Bug Fixes

* **openai:** attribute Vercel AI Gateway client spans ([#460](https://github.com/future-agi/traceAI/issues/460)) ([2990414](https://github.com/future-agi/traceAI/commit/29904145b76b6b5f6c963a0372092e9d567e8b19))
* **openai:** attribute Z.ai chat requests to their provider ([#467](https://github.com/future-agi/traceAI/issues/467)) ([c811861](https://github.com/future-agi/traceAI/commit/c8118613361ada6e29997a2171bb14dc983e79b5))

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
