# Changelog

## [0.2.2] - pending release
### Fix
- For supplied configs whose cap equals OpenInference's dataclass-declared default, an unset, blank, `-1`, or non-integer `OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH` no longer bypasses the FI ceiling: the cap is the smaller of that default and the FI cap (0 when FI is unset). This includes explicitly supplying the default value; the reference default is read without constructing another environment-dependent config.
- Nondefault nonnegative caller caps and valid nonnegative decimal OpenInference environment caps retain their existing behavior; a set FI cap can only tighten them. Without a supplied config, the cap still comes only from FI, or 0 when unset.
- The wheel and sdist now include the Apache-2.0 `LICENSE` file.

## [0.2.1] - pending release
### Changed
- Images are no longer exported by default as base64 payloads. To allow them within a positive cap, set `FI_BASE64_IMAGE_MAX_LENGTH` or supply `base64_image_max_length` on an OpenInference config. The cap rules below and content-hiding flags still apply.

### Fix
- With no supplied OpenInference config, the cap comes from `FI_BASE64_IMAGE_MAX_LENGTH`, or 0 when unset. `OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH` is not read for this cap.
- A supplied config's cap is limited to the smaller of its value and the FI cap when `OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH` is unset and the value equals OpenInference's default. With that environment variable unset, the same limit applies if OpenInference's default cannot be read. Otherwise, the supplied cap is kept unless `FI_BASE64_IMAGE_MAX_LENGTH` tightens it.
- Invalid base64 image caps resolve to 0. A set `FI_BASE64_IMAGE_MAX_LENGTH` can only lower a supplied cap.

### Dependencies
- Requires fi-instrumentation-otel>=1.3.0.

## [0.2.0](https://github.com/future-agi/traceAI/compare/traceai-agno-v0.1.0...traceai-agno-v0.2.0) (2026-11-05)


### ⚠ BREAKING CHANGES

* **agno:** require Agno >=2.7.2 with OpenInference >=1.1.0. Configure export/project settings on either the supplied provider or the endpoint path, not both; unconfigured and externally instrumented setup now raises instead of silently reporting success.

### Bug Fixes

* **agno:** honor FI privacy and preserve retrieval and stream lifecycle ([#572](https://github.com/future-agi/traceAI/issues/572)) ([983671a](https://github.com/future-agi/traceAI/commit/983671a1bc37f1cf30ba6ab73009c874f6425079))
* **agno:** repair delegated tracing setup ([#484](https://github.com/future-agi/traceAI/issues/484)) ([69a4b95](https://github.com/future-agi/traceAI/commit/69a4b950c66afc002e7bf0264222a9fda031b857))
* **agno:** repair recalled privacy and stream lifecycle defects ([#533](https://github.com/future-agi/traceAI/issues/533)) ([a71f60b](https://github.com/future-agi/traceAI/commit/a71f60b312dd53a5149935da926aa4e007b0cabb))
