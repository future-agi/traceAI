# Changelog

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to npm; one-time bootstrap patch for the automated release pipeline.
- Declare `@opentelemetry/sdk-trace-base` as a dependency. The span processor imports it at runtime, so a clean install without it failed with "Cannot find module".
