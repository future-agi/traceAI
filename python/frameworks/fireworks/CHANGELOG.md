# Changelog

## Unreleased

- With `hide_inputs`, `hide_input_messages`, `hide_input_text` or `hide_input_images`, the whole-response `output.value`, including streamed chunks, is built from response-side fields (choices, usage, the raw output completion) while any input hide setting is on, so `prompt_fragments` and `prompt_token_ids` are not recorded.

## [0.1.3] - 2026-11-12

### Changed
- traceai-fireworks now requires fi-instrumentation-otel 1.3.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

## [0.1.2](https://github.com/future-agi/traceAI/compare/traceai-fireworks-v0.1.1...traceai-fireworks-v0.1.2) (2026-11-05)


### Bug Fixes

* **fireworks:** prefer a single OpenAI-compatible trace ([#367](https://github.com/future-agi/traceAI/issues/367)) ([84aa857](https://github.com/future-agi/traceAI/commit/84aa857c90be0eb0aebce7bc31137dd6ddbeeee6))
* **fireworks:** protect credentials and finalize legacy stream spans ([#570](https://github.com/future-agi/traceAI/issues/570)) ([01c1f59](https://github.com/future-agi/traceAI/commit/01c1f59c5790ec2f7f785f228347c95c44703809))
* **openai:** close privacy and SDK lifecycle release blockers ([#571](https://github.com/future-agi/traceAI/issues/571)) ([d7e5555](https://github.com/future-agi/traceAI/commit/d7e5555b60faa8b77d8f0db05942e91092fcfe94))

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.
