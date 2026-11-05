# Changelog

## [0.2.0](https://github.com/future-agi/traceAI/compare/traceai-agno-v0.1.0...traceai-agno-v0.2.0) (2026-11-05)


### ⚠ BREAKING CHANGES

* **agno:** require Agno >=2.7.2 with OpenInference >=1.1.0. Configure export/project settings on either the supplied provider or the endpoint path, not both; unconfigured and externally instrumented setup now raises instead of silently reporting success.

### Bug Fixes

* **agno:** honor FI privacy and preserve retrieval and stream lifecycle ([#572](https://github.com/future-agi/traceAI/issues/572)) ([983671a](https://github.com/future-agi/traceAI/commit/983671a1bc37f1cf30ba6ab73009c874f6425079))
* **agno:** repair delegated tracing setup ([#484](https://github.com/future-agi/traceAI/issues/484)) ([69a4b95](https://github.com/future-agi/traceAI/commit/69a4b950c66afc002e7bf0264222a9fda031b857))
* **agno:** repair recalled privacy and stream lifecycle defects ([#533](https://github.com/future-agi/traceAI/issues/533)) ([a71f60b](https://github.com/future-agi/traceAI/commit/a71f60b312dd53a5149935da926aa4e007b0cabb))
