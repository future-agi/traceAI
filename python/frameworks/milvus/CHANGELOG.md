# Changelog

## [0.1.2](https://github.com/future-agi/traceAI/compare/traceai-milvus-v0.1.1...traceai-milvus-v0.1.2) (2026-11-05)


### Bug Fixes

* **milvus:** enforce privacy and repair client tracing lifecycle ([#402](https://github.com/future-agi/traceAI/issues/402)) ([3ff1a3a](https://github.com/future-agi/traceAI/commit/3ff1a3aa545bcc4391c23e4a6cbf4d732e91a7a4))
* **milvus:** redact PII from exception telemetry ([#596](https://github.com/future-agi/traceAI/issues/596)) ([427f74a](https://github.com/future-agi/traceAI/commit/427f74a4063dd6ef828bde82a429000ab42ad798))

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.0] - 2025-01-24

### Added
- Initial release
- Instrumentation for Milvus/PyMilvus
- Support for search, query, insert, upsert, delete operations
- Support for MilvusClient and Collection APIs
