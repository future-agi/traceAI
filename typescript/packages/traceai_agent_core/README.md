# Coding-agent core

`@traceai/agent-core` supplies shared configuration, secure setup, status and finalized OpenTelemetry turn tracing for maintained Future AGI coding-agent integrations. Host adapters own installation and source-event translation; this package does not instrument a host by itself.

## Configuration and lifecycle

- `resolveConfig` resolves defaults, the host's `futureagi.json`, an explicitly selected project override, `FI_*` environment variables and invocation flags, in that order. It never reads `.env`, shell profiles, authentication caches or unrelated transcripts.
- `setup` validates a complete key pair against the exact OTLP traces destination before storing it in the owner-only central credentials file. Native installation is a separate adapter transaction: a configured profile is not a registered or activated integration.
- `inspectStatus` reports configuration provenance without key values, registration, observed activation, credential acceptance and export diagnostics separately. HTTP acceptance is not product readback.
- `disable` writes a persistent fence without requiring credentials or network access. Collection and sending consult that fence, including stale in-process exporters.
- `CoreAdapter` defines host discovery, installation, inspection, uninstall and optional managed-run operations. A host without a qualified adapter reports a capability gap instead of installing speculatively.

The default credentials path is `${XDG_CONFIG_HOME:-$HOME/.config}/futureagi/agents/credentials.json`; `FI_AGENT_CONFIG_DIR` overrides the shared directory. Credential files and the installation manifest are mode `0600`, with private directories created as `0700`. Project files contain profile references, never credentials. Existing symlinks, non-owner files, unsafe modes and conflicting saves are rejected. Windows is not supported until owner-only ACL handling is available.

`FI_API_KEY` and `FI_SECRET_KEY` must be supplied together. `FI_BASE_URL` is an API origin; `FI_OTLP_ENDPOINT` is an exact traces URL and takes precedence. Credentials are not destination consent: repository-selected endpoints, project names and unrelated profiles require a matching protected approval, or explicit trusted routing overrides. Project-bound profiles require the same canonical project directory. Nested working directories inherit the nearest project settings without crossing a repository boundary. Unsafe or malformed repository settings are ignored with `PROJECT_CONFIG_IGNORED`; protected user settings and credentials still fail closed.

Setup defaults are scope-aware; repeated project setup and credential rotation retain the approved project account and content opt-out. Edited or transplanted repository destination profiles are rejected before validation. Setup advances the same persistent epoch used by disable, fencing pending captures when privacy or routing changes. Status reports `content-on-by-default` or `structure-only`. Delivery evidence is route/epoch scoped; Hermes additionally isolates native-home/account evidence. Cursor Cloud reads credentials only from its environment and never persists them.

## Privacy

Content capture is on by default after setup. Captured prompts, model and tool input/output may include file text and secrets the agent reads. Set `FI_AGENT_CAPTURE_CONTENT=false` or persist `captureContent:false` to capture structure and usage without content. Existing `FI_HIDE_*` restrictions are preserved, including `FI_HIDE_ERROR_DETAILS`.

Captured fields are capped at 20,000 characters. Known Future AGI credentials and common secret patterns are redacted on a best-effort basis; this is not a guarantee that arbitrary secrets can be detected. A project cannot raise a profile's approved content ceiling. Pending content stays in bounded process memory, not in an on-disk journal.

## Turn tracing

`createTurnTracer({ config })` returns a private provider-backed tracer. `startTurn` requires source session and turn anchors. Its turn handle creates model, tool, subagent or compaction children and finalizes the turn on an actual terminal event. It does not register a global OpenTelemetry provider or context manager, run resource detectors or alter model requests.

Use `kind:'llm', fidelity:'native-request'` only for a proven model request/attempt. Persisted request records may use `recorded-request`. An identified source assistant message uses `kind:'generation', fidelity:'reconstructed-message', timing:'reconstructed'`: it renders as an LLM generation without claiming measured request timing. A prompt/answer pair alone is not evidence of a generation.

Supply cache/reasoning inclusion rules and a stable usage plan. Request/attempt model spans own additive counters. For a `turn` plan, the last source generation owns the aggregate exactly once; when no generation exists, the AGENT root owns it. The root otherwise keeps nonadditive summary fields. A compaction child may opt into `usageContribution:'exclusive'` with `fidelity:'aggregate-turn'` in a request/attempt plan only when its counters do not overlap model children. Unknown values remain absent.

Costs use `gen_ai.cost.total`, with reported/estimated provenance. `timeToFirstTokenMs` is exported as `gen_ai.server.time_to_first_token` only for native measured requests, never reconstructed generations. Tools carry `tool.name` and, when supplied, the actual `toolCallId` as `tool_call.id`; deterministic work IDs remain separate. An unrecovered final model failure makes a generic terminal root ERROR; a later successful retry recovers it. Adapters must still pass authoritative terminal outcomes, including errors before any native child is observed. Unfinished children and shutdown without completion evidence are incomplete, not successful.

The finalized route uses standard OTLP JSON, `project_type=observe`, matching `project_name` and `X-Project-Name`, and both authentication headers. Source-bound HMAC identities are destination-specific. A bounded in-memory dedup window does not establish backend exactly-once delivery.

Delivery is bounded to 256 queued spans, 4 MiB of estimated serialized queue size and 256 KiB per span. A periodic flush sends up to 64 spans. `drain()` and shutdown join an active send and drain later batches under one five-second budget, including diagnostic writes. A `413` splits a batch within that same budget. Policy and disable fences are rechecked before every HTTP batch, including split requests. Authentication rejection, rate limiting, transport ambiguity and partial rejection are distinct metadata-only diagnostics. No attempted member is retried after ambiguity, rate limiting or partial success. Work left at the deadline is dropped and accounted for. `discard()` irreversibly fences buffered content before asynchronous shutdown. There is no durable-delivery promise.

Protected file updates use `proper-lockfile` leases with bounded waiting, heartbeat renewal, stale-owner recovery and no-follow checks. `updateJson` holds one lease across read/merge/write. `writeProtectedUnderLock` is for an adapter that already owns the common external session lease; it does not acquire a nested lock. Readable nonsecret host settings may opt into `privateFile:false`, but shared-writable files are never accepted. Goose-style larger metadata files may explicitly raise the size bound to at most 1 MiB; credential/config defaults stay at 64 KiB. Legacy ownerless `.lock` files are left inert rather than deleted.

`uninstall` disables first, calls the adapter's ownership-aware cleanup, and removes only unchanged setup-owned settings and empty directories that setup created. It preserves credentials, preexisting settings and host edits. CLI consumers should call this core lifecycle operation rather than the adapter's uninstall method alone.

## Existing preview contracts

The identity, privacy, usage, schema and local-client entry points retain the separate preview contracts:

- `@traceai/agent-core/identity`: stable source-work IDs from proven anchors.
- `@traceai/agent-core/privacy`: structural allowlists and explicit category ceilings; this preview minimizer remains structure-only by default.
- `@traceai/agent-core/usage`: declared counter inclusions and immutable usage-plan keys.
- `@traceai/agent-core/client`: one bounded local socket attempt, with no credential lookup, HTTP request or fallback file.
- `@traceai/agent-core/schema`: canonical types and size limits.

The preview client's durable-local acknowledgement is not remote acceptance. Its runtime's `immutable-agent-v1`, content-persistence and `HELD_BACKEND_CONTRACT` holds are not changed or satisfied by the separate `finalized-standard-otlp-v1` route. This package does not start or replace that preview runtime.

The package provides ESM and CommonJS on Node 22.16–22.x and 24.x. Host-version qualification is separate from this runtime requirement.
