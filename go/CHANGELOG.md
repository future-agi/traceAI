# Go SDK changelog

## go/auto 0.2.0 (pending release)

Released with the tag `go/auto/v0.2.0`.

### Added

- On agent-owned providers, sampled spans carry the trace's keep probability as the double attribute `fi.sampling.ratio` when it is strictly between 0 and 1, and a local fractional ratio decision writes `fi=r:<ratio>` to W3C tracestate (#713). Parent-based samplers copy a valid ratio from a sampled local or remote parent's `fi` entry at any root rate. An absent attribute means `1.0`. Roots decided at `0` or `1`, dropped or record-only spans and application-owned providers get no ratio; an invalid or absent parent starts a root without carrying its tracestate forward. Wire admission reserves one attribute slot for the ratio, and export privacy recovery keeps a valid ratio without restoring hidden content.

### Fixed

- Go auto instrumentation honors `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` when it creates a provider without an explicit sample rate (#711). Explicit FI rates keep parent-based ratio sampling; application-owned samplers remain unchanged, and invalid OTEL sampler settings fall back to the default with one warning.

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
