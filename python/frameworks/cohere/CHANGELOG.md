# Changelog

## [0.1.2] - pending release

### Changed
- traceai-cohere now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- AsyncClient chat streams are traced; chat calls are also traced when the history holds SDK message objects or when history, tools, documents or preamble is set to None; and typed embedding responses other than base64 record a preview of the first vector.

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.
