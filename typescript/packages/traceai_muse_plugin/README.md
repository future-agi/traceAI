# Future AGI for Muse Code

A native Muse plugin installed by the unified `@traceai/agents` CLI. The plugin observes turns through argv-array hooks; it does not change model providers, tool permissions, or workspace trust.

```sh
npx @traceai/agents setup muse --project coding-agents
muse plugins inspect futureagi-tracing
muse plugins approve futureagi-tracing
```

Restart Muse after approving the hooks. Setup never approves executable capabilities. `status muse --check` distinguishes registration, approval, observed activation, and endpoint acceptance.

This package is private until its release gates are complete. See `qualification.json` for the supported surface and outstanding gates; the commands above describe the intended published interface, not registry availability.

## Configuration and privacy

- The shared core validates the Future AGI key pair and writes owner-only credential/profile files.
- The native launcher pins an absolute Node path, persistent bundled runtime, and configuration discovery paths. No credentials are embedded in the plugin, argv, or project files. Hooks do not depend on the npx cache or download code at runtime.
- Muse removes `FI_*` and `XDG_*` from hook environments. Run setup to persist configuration changes; shell-only overrides are not forwarded to installed hooks. `disable muse` is the persistent kill switch.
- Content is enabled by default. Use `setup muse --no-capture-content` for structure-only traces. Content can include source files and secrets read by the agent. Best-effort credential redaction is not a security guarantee.
- Pending normalized content is encrypted with a per-profile key and authenticated against session, turn, destination and disable generation. Files are owner-only, bounded to 128 entries, and expire after 24 hours on later hook activity. A turn holds at most 64 children and 24 KB of UTF-8 content. There is no raw hook journal.
- Terminal exports use the core's finalized standard OTLP route, not the held immutable-preview protocol. Delivery is bounded and fail-open, without blind retries after ambiguity or partial acceptance.

## Signal fidelity

A prompt creates a turn root grouped by session. Pre/PostLLMCall pairs identify real requests and attempts, with reported input/output token counts where available. Their content consists of Muse's partial message and output previews, explicitly marked partial, not full model messages. Tool calls carry their source call identities and observed hook intervals. Compaction is a child activity. Child-session hooks without verified parent context produce an explicit gap; the plugin never invents child ancestry.

Muse hook usage counters are cumulative within a turn. The adapter computes per-request deltas only across contiguous first-attempt successes; retries and gaps retain only a nonadditive summary.

The adapter does not infer absent usage or cost, turn silence into success, or echo-provider activity into an LLM generation. Muse skips hooks over 256 KiB without invoking the adapter. Status exposes this blind spot plus detected gaps; it cannot count unknown events.

## Development

Build and test only in the cloud validation environment. `test/e2b.sh` builds the workspace dependencies, packages the plugin, verifies the pinned real Muse binary checksum, isolates all execution from non-loopback networking, and exercises the native plugin lifecycle against synthetic credentials and local capture/provider servers. No real credentials are required.

The fake provider is implemented against Muse's documented first-party model catalog and completion protocol, not an assumption that Muse accepts arbitrary OpenAI-compatible settings. Protocol reference: <https://github.com/meta-models/muse-code-sdk/blob/main/clients/sdk-quickstart/src/provider.ts>. Native manifest and hook references: <https://meta-models.github.io/muse-code-sdk/next/guides/plugins/reference/manifest/> and <https://meta-models.github.io/muse-code-sdk/next/guides/plugins/reference/hook-events/>.
