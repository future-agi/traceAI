# Changelog

## [0.1.3] - pending release

### Changed
- traceai-deepseek now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

## [0.1.2](https://github.com/future-agi/traceAI/compare/traceai-deepseek-v0.1.1...traceai-deepseek-v0.1.2) (2026-11-05)


### Bug Fixes

* **deepseek:** migrate to the shared OpenAI tracing route ([#376](https://github.com/future-agi/traceAI/issues/376)) ([9864092](https://github.com/future-agi/traceAI/commit/986409265a6270ae3821b6988f5af12af96bb505))
* **deepseek:** protect raw responses and distinguish cancelled streams ([#582](https://github.com/future-agi/traceAI/issues/582)) ([4a746c4](https://github.com/future-agi/traceAI/commit/4a746c4827cdf13db0338bff6db5a1e815a2cbc9))
* **openai:** close privacy and SDK lifecycle release blockers ([#571](https://github.com/future-agi/traceAI/issues/571)) ([d7e5555](https://github.com/future-agi/traceAI/commit/d7e5555b60faa8b77d8f0db05942e91092fcfe94))

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.
