# Changelog

## [Unreleased]

### Changed
- Defers to the shared OpenAI instrumentor (traceai-openai) while it is on, so a call yields one span, and declares `DEFERS_TO_SHARED_OPENAI`, which the core reads to turn the integration on by default (an older release without it stays off by default, so a call never yields two spans). Deferral needs a traceai-openai with the provider extensions.

## [0.1.2] - 2026-11-12

### Changed
- traceai-xai now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.
