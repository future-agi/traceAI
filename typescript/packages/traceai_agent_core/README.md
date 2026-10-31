# Coding-agent contracts

`@traceai/agent-core` defines the shared identity, privacy, usage and local-client contracts for Future AGI coding-agent integrations. It does not instrument an agent or send data to Future AGI.

## Entry points

- The root export contains canonical record types, validation, privacy minimization, identity and usage functions.
- `@traceai/agent-core/identity` derives stable source-work IDs from a destination identity key and proven source anchors. Parser, credential, policy and route versions are not identity inputs.
- `@traceai/agent-core/privacy` applies structural allowlists and an explicit category ceiling. Structure-only is the default. Redaction of opted-in text is best effort, not a guarantee that arbitrary secrets can be detected.
- `@traceai/agent-core/usage` normalizes declared token inclusions and derives immutable per-turn usage-plan keys. Missing counters stay unknown.
- `@traceai/agent-core/client` makes one bounded attempt to submit an already minimized fact to an owner-protected local socket. It never loads credentials, opens SQLite, exports HTTP, or writes fallback files.
- `@traceai/agent-core/schema` provides the canonical types and size limits. JSON schemas are included in the package's `schema` directory.

The local client's acknowledgement distinguishes a durable local commit from an unavailable runtime or timeout. It is not an acknowledgement of remote acceptance or persistence. A timeout can occur after the runtime committed the fact; any later replay must retain the same identity.

## Usage ownership

One originating turn uses a request, attempt or turn-only accounting plan. Nonadditive summaries use `contribution: 'none'`; their counters must not be copied into additive token or cost fields. Equal-value requests with different source anchors remain distinct requests.

## Host requirements

Use a version-qualified observation surface and its documented neutral response. A generic JSON success response is not a safe substitute for a host's hook protocol. The client has no permission-decision API and must not change an agent's output or exit status.

The package supports ESM and CommonJS on Node 22.16–22.x and 24.x. Host-engine qualification is separate from the local runtime's engine requirement.
