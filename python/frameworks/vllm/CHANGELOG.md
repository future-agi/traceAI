# Changelog

## [0.1.2] - 2026-11-19

### Changed
- The package declares `DEFERS_TO_SHARED_OPENAI`, which the core reads to turn the integration on by default (an older release without it stays off by default, so a call never yields two spans).
- With `hide_inputs`, `hide_input_messages`, `hide_input_text` or `hide_input_images`, the whole-response `output.value`, including streamed chunks, is built from response-side fields (choices, usage, ids) while any input hide setting is on, so `prompt_text` and `prompt_token_ids` are not recorded.

## [0.1.1](https://github.com/future-agi/traceAI/compare/traceai-vllm-v0.1.0...traceai-vllm-v0.1.1) (2026-11-05)


### Bug Fixes

* **openai:** close privacy and SDK lifecycle release blockers ([#571](https://github.com/future-agi/traceAI/issues/571)) ([d7e5555](https://github.com/future-agi/traceAI/commit/d7e5555b60faa8b77d8f0db05942e91092fcfe94))
* **openai:** migrate vLLM and attribute Ollama clients ([#424](https://github.com/future-agi/traceAI/issues/424)) ([cd4ea17](https://github.com/future-agi/traceAI/commit/cd4ea177ac4c5426a9df7aeff870154656f71763))
