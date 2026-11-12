## [0.1.4] - pending release

### Changed
- traceai-google-genai now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- Preserve distinct parts in streaming responses so parallel function calls, repeated calls to the same tool, and multimedia parts are not overwritten or merged into text parts in exported traces.
- Streamed traces combine adjacent plain-text fragments without merging function calls or multimedia parts into text.

## [0.1.3] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.2 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.0] - 2025-07-04
### Feature
- Added Support for Google Gen AI

## [0.1.1] - 2025-12-11
### Feature
- Added Support for Function Calling
- Added Support for MultiMedia Request Attributes

