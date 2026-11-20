# go/auto changelog

Release history of `github.com/future-agi/traceAI/go/auto`. Each version is published as the tag `go/auto/v<version>`.

## Unreleased

### Added

- `auto.Attach(name)` lets a dedicated module's blank-import package send its spans through the agent's route, at the application's global SDK provider or the agent's private one. The module is reported as `auto` instead of `manual` while the agent is active or armed, follows `FI_INSTRUMENTATIONS` and `OTEL_SDK_DISABLED` without activating the agent when it is not allowed, applies the wire rules (structural capture, hidden image references, every `FI_HIDE_*` switch) to the span name, attributes, events, links and status of the copy the application's own provider exports, with known credentials replaced as the span ends (its events and links are added then) and, in the copy sent to Future AGI, at export; a recorded stack trace is cut to 2 048 bytes after the replacement, and the copy sent to Future AGI keeps the provider's attribute value-length limit, including what a module writes through the span it reads back from the context, a rename and a late link, never replaces `http.DefaultTransport`, and can wait for its background spans for up to half of the shutdown budget. `go/eino/auto` uses it. No dependency was added.
- The `FI_HIDE_*` rules cover a retriever's query (`gen_ai.retrieval.query`, with the input switches) and retrieved document content and metadata (`retrieval.documents.N.document.content` and `.document.metadata*`, with the output switches); they are replaced by `__REDACTED__`.

## [0.2.0] - 2026-11-18

### Added

- On agent-owned providers, sampled spans carry the trace's keep probability as the double attribute `fi.sampling.ratio` when it is strictly between 0 and 1, and a local fractional ratio decision writes `fi=r:<ratio>` to W3C tracestate (#713). Parent-based samplers copy a valid ratio from a sampled local or remote parent's `fi` entry at any root rate. An absent attribute means `1.0`. Roots decided at `0` or `1`, dropped or record-only spans and application-owned providers get no ratio; an invalid or absent parent starts a root without carrying its tracestate forward. Wire admission reserves one attribute slot for the ratio, and export privacy recovery keeps a valid ratio without restoring hidden content.

### Fixed

- Go auto instrumentation honors `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` when it creates a provider without an explicit sample rate (#711). Explicit FI rates keep parent-based ratio sampling; application-owned samplers remain unchanged, and invalid OTEL sampler settings fall back to the default with one warning.

## [0.1.0] - 2026-11-17

### Added

- First release: zero-code tracing of AI calls for Go applications (#707).
