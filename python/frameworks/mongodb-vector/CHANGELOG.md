# Changelog

## [0.1.2](https://github.com/future-agi/traceAI/compare/traceai-mongodb-v0.1.1...traceai-mongodb-v0.1.2) (2026-11-05)


### Bug Fixes

* **mongodb:** preserve bulk-write errors and reentrant span lifetimes ([#597](https://github.com/future-agi/traceAI/issues/597)) ([b8d56fa](https://github.com/future-agi/traceAI/commit/b8d56fa8c9773bd4bbc2e9eb8f0275adf9a4f884))
* **mongodb:** use maintained command listener tracing ([#399](https://github.com/future-agi/traceAI/issues/399)) ([4a75f7a](https://github.com/future-agi/traceAI/commit/4a75f7a27efdc1600cbaab58dd905bbb44f1ac65))

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.0] - 2025-01-24

### Added
- Initial release
- Instrumentation for MongoDB Atlas Vector Search
- Support for $vectorSearch aggregation stage
- Support for standard CRUD operations
