# Coding-agent core

`@traceai/agent-core` supplies shared configuration, secure setup, status and finalized OpenTelemetry turn tracing for maintained Future AGI coding-agent integrations. Host adapters own installation and source-event translation; this package does not instrument a host by itself.

## Configuration and lifecycle

- `resolveConfig` resolves defaults, the host's `futureagi.json`, an explicitly selected project override, `FI_*` environment variables and invocation flags, in that order. It never reads `.env`, shell profiles, authentication caches or unrelated transcripts.
- `setup` validates a complete key pair against the exact OTLP traces destination before storing it in the owner-only central credentials file. Native installation is a separate adapter transaction: a configured profile is not a registered or activated integration.
- `inspectStatus` reports configuration provenance without key values, registration, observed activation, credential acceptance and export diagnostics separately. HTTP acceptance is not product readback.
- `disable` writes a persistent fence without requiring credentials or network access. Collection and sending consult that fence, including stale in-process exporters.
- `CoreAdapter` defines host discovery, installation, inspection, uninstall and optional managed-run operations. A host without a qualified adapter reports a capability gap instead of installing speculatively.

The default credentials path is `${XDG_CONFIG_HOME:-$HOME/.config}/futureagi/agents/credentials.json`; `FI_AGENT_CONFIG_DIR` overrides the shared directory. Credential files and the installation manifest are mode `0600`, with private directories created as `0700`. Project files contain profile references, never credentials. Existing symlinks, non-owner files, unsafe modes and conflicting saves are rejected. Windows is not supported until owner-only ACL handling is available.

`FI_API_KEY` and `FI_SECRET_KEY` must be supplied together. `FI_BASE_URL` is an API origin; `FI_OTLP_ENDPOINT` is an exact traces URL and takes precedence. Saved keys are bound to their approved destination and project; a repository cannot redirect them. Project-bound profiles also require the same canonical project directory.

## Privacy

Content capture is on by default after setup. Captured prompts, model and tool input/output may include file text and secrets the agent reads. Set `FI_AGENT_CAPTURE_CONTENT=false` or persist `captureContent:false` to capture structure and usage without content. Existing `FI_HIDE_*` restrictions are preserved, including `FI_HIDE_ERROR_DETAILS`.

Captured fields are capped at 20,000 characters. Known Future AGI credentials and common secret patterns are redacted on a best-effort basis; this is not a guarantee that arbitrary secrets can be detected. A project cannot raise a profile's approved content ceiling. Pending content stays in bounded process memory, not in an on-disk journal.

## Turn tracing

`createTurnTracer({ config })` returns a private provider-backed tracer. `startTurn` requires source session and turn anchors. Its turn handle creates model, tool, subagent or compaction children and finalizes the turn on an actual terminal event. It does not register a global OpenTelemetry provider or context manager, run resource detectors or alter model requests.

Use `fidelity:'native-request'` only for a proven model request/attempt. Reconstructed messages remain explicitly non-request work. Supply cache/reasoning inclusion rules with usage. Request/attempt spans own additive model counters; turn summaries use nonadditive metadata. Unknown values remain absent. Unfinished children and shutdown without a terminal event are incomplete, not successful.

The finalized route uses standard OTLP JSON, `project_type=observe`, matching `project_name` and `X-Project-Name`, and both authentication headers. Source-bound HMAC identities are destination-specific. A bounded in-memory dedup window does not establish backend exactly-once delivery.

Delivery is bounded to 256 queued spans, 4 MiB of estimated serialized queue size and 256 KiB per span. A flush sends up to 64 spans within a five-second network budget. A `413` splits a batch within that same budget. Authentication rejection, rate limiting, transport ambiguity and partial rejection are distinct metadata-only diagnostics. No automatic retry is made after ambiguity, rate limiting or partial success, so accepted members are never blindly resent. Shutdown drops remaining work after its bounded flush and records that loss. There is no crash-recovery or durable-delivery promise.

## Existing preview contracts

The identity, privacy, usage, schema and local-client entry points retain the separate preview contracts:

- `@traceai/agent-core/identity`: stable source-work IDs from proven anchors.
- `@traceai/agent-core/privacy`: structural allowlists and explicit category ceilings; this preview minimizer remains structure-only by default.
- `@traceai/agent-core/usage`: declared counter inclusions and immutable usage-plan keys.
- `@traceai/agent-core/client`: one bounded local socket attempt, with no credential lookup, HTTP request or fallback file.
- `@traceai/agent-core/schema`: canonical types and size limits.

The preview client's durable-local acknowledgement is not remote acceptance. Its runtime's `immutable-agent-v1`, content-persistence and `HELD_BACKEND_CONTRACT` holds are not changed or satisfied by the separate `finalized-standard-otlp-v1` route. This package does not start or replace that preview runtime.

The package provides ESM and CommonJS on Node 22.16–22.x and 24.x. Host-version qualification is separate from this runtime requirement.
