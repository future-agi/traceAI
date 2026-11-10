# Changelog

All notable changes to traceAI-pinecone will be documented in this file.

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.0] - 2025-01-24

### Added
- Initial release
- Instrumentation for Pinecone v3+ and v4+ APIs
- Support for `query`, `upsert`, `delete`, `fetch`, `update`, `describe_index_stats` operations
- OpenTelemetry semantic conventions for vector database operations
- Namespace support
- Filter expression tracking
- Result metrics (count, scores, IDs)
