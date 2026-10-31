# Go SDK changelog

## Unreleased

### Added

- Go SDK index with module selection, installation, a complete OpenAI quickstart, privacy notes and the per-module release tag scheme.
- Go CI for all five modules at the minimum and current stable Go versions: module identity, portable dependencies, tidy, vet, race tests, example builds and package documentation.

### Changed

- All modules now declare Go 1.26.0 as their minimum. This raises the Anthropic module's minimum from Go 1.24; other modules already required Go 1.26.0.
- Module READMEs consistently distinguish pre-release `@dev` installs from tagged `@latest` installs.

### Fixed

- Gemini installation instructions explicitly install the Google Gen AI SDK, so fresh consumers can build the documented usage without missing `go.sum` entries.

No Go release is published by these changes. Each module's release history will be maintained in its own directory when it is released.
