

## [0.1.11] - pending release

### Changed
- traceai-groq now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- Groq spans record only known inference parameters, so request headers (including credentials in extra_headers), extra_body and extra_query are no longer recorded in span attributes.
- Messages passed as an iterator reach Groq intact, and traced completion streams keep their with/close behaviour.

## [0.1.10] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.9 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

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
