

## [0.1.11] - 2026-10-31
### Fixed
- Bound legacy `google-cloud-aiplatform` support to `>=1.61.0,<=1.101.0`, retaining the generative AI modules and allowing the protobuf version required by the tracing dependencies.
- Default the legacy example to `us-central1` and document replacing `global` with a supported region on older SDKs.
- Replace the separate `vertexai` distribution in setup instructions and example requirements with `google-cloud-aiplatform`.
- Direct new Vertex AI applications to `traceAI-google-genai` with a `vertexai=True` client, and document migration from the deprecated SDK.

## [0.1.8] - 2025-07-08
### Feature
- Added support for Vertex AI's new modals

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
- Added Support for Audio and Image Generation models
