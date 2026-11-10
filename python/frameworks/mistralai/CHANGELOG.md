## [0.1.10](https://github.com/future-agi/traceAI/compare/traceai-mistralai-v0.1.9...traceai-mistralai-v0.1.10) (2026-11-05)


### Bug Fixes

* **mistralai:** adapt native telemetry for current Python SDK ([#458](https://github.com/future-agi/traceAI/issues/458)) ([cb2e4c6](https://github.com/future-agi/traceAI/commit/cb2e4c6e966dc8d38a655800c55e034276be8662))
* **mistralai:** preserve model attribution and trace classifiers ([#590](https://github.com/future-agi/traceAI/issues/590)) ([629e9c9](https://github.com/future-agi/traceAI/commit/629e9c958b21ab06490a64f5cda9b7fd92d552ba))

## [0.1.9] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.8 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.
- Require `mistralai<2`. The instrumentor imports `mistralai.utils`, which mistralai 2.0 removed, so it failed to import with mistralai 2.x and 3.x.

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
