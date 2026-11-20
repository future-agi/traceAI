# Go SDK changelog

Released modules keep their release history in their own folder: `go/auto` in [`go/auto/CHANGELOG.md`](auto/CHANGELOG.md).

## Unreleased

### Added

- Eino programs are traced from one blank import, `github.com/future-agi/traceAI/go/eino/auto`: agent, chain, tool and model spans reach Future AGI through the zero-code agent with or without a tracer provider of the application's own. Programs that do not import it download no Eino dependencies. `go/eino` adds `NewDefaultHandler`, `HandledManually`, `DefaultNeeded` and `RunTraced`, and its `Handler` implements `Needed`, so the blank import steps aside for exactly the runs an application handler handles. The `FI_HIDE_*` switches and the replacement of known credentials apply to the copy of a span the application's own provider exports as well as to the copy sent to Future AGI.
- Go SDK index with module selection, installation, a complete OpenAI quickstart, privacy notes and the per-module release tag scheme.
- Go CI for all five modules at the minimum and current stable Go versions: module identity, portable dependencies, tidy, vet, race tests, example builds and package documentation.

### Changed

- All modules now declare Go 1.26.0 as their minimum. This raises the Anthropic module's minimum from Go 1.24; other modules already required Go 1.26.0.
- Module READMEs consistently distinguish pre-release `@dev` installs from tagged `@latest` installs.

### Fixed

- Gemini installation instructions explicitly install the Google Gen AI SDK, so fresh consumers can build the documented usage without missing `go.sum` entries.

No Go release is published by these changes. Each module's release history will be maintained in its own directory when it is released.
