# Changelog

## [0.1.1] - pending release

### Fixed

- The README shows how to install the package from npm instead of building it from source.

## [0.1.0] - 2026-11-09

- Add workflow and node execution attempt spans for `n8n-core >=2.16.1 <3`.
- Preserve workflow results, cancellable promises, parent context and retry
  behavior; handle resolved errors and cancellation without duplicate span ends.
- Export execution metadata without item payloads, credentials or error messages.
- Support automatic CommonJS loading and manual instrumentation, with real-engine
  integration tests and a self-hosted setup guide.
