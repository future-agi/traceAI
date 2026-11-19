# Changelog

All notable changes to traceAI-chromadb will be documented in this file.

## [0.2.0](https://github.com/future-agi/traceAI/compare/traceai-chromadb-v0.1.1...traceai-chromadb-v0.2.0) (2026-11-05)


### ⚠ BREAKING CHANGES

* **chromadb:** Python >=3.10, ChromaDB >=0.5.1 and fi-instrumentation-otel >=1.2.2 are now required. The previous floors (Python 3.9, Chroma 0.4.0) could not install or import with any supported tracing core; upgrade those environments before upgrading this integration.

### Bug Fixes

* **chromadb:** repair collection tracing and enforce privacy ([#341](https://github.com/future-agi/traceAI/issues/341)) ([a54cd25](https://github.com/future-agi/traceAI/commit/a54cd2555d329c63a7778bd65a72556de729ecc5))
* **chromadb:** respect collection privacy and preserve nested queries ([#587](https://github.com/future-agi/traceAI/issues/587)) ([9471cba](https://github.com/future-agi/traceAI/commit/9471cba9f3a149b8ef95602025c05332ef277468))

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.0] - 2025-01-24

### Added
- Initial release
- Instrumentation for ChromaDB v0.4.x and v0.5.x
- Support for `add`, `query`, `get`, `update`, `upsert`, `delete`, `count`, `peek` operations
- OpenTelemetry semantic conventions for vector database operations
- Query type detection (embedding vs text)
- Result metrics (count, IDs, distances)
- Collection metadata tracking
