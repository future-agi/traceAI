## [0.1.11] - 2026-10-31
### Fixed
- Keep Messages instrumentation available when the SDK has no legacy Completions API, and restore token-count hooks on teardown.
- Preserve native tool inputs, stream items, close behavior, context-manager results, errors, and cancellation across synchronous and asynchronous calls.
- Finalize stream spans once without holding lifecycle locks during SDK serialization or span export; isolate malformed telemetry and bound pending event accumulation.
- Apply the core error-detail policy and retain valid output attributes when individual fields cannot be recorded.

### Changed
- Require fi-instrumentation-otel 1.2.1 or later for the error-detail policy API.

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
