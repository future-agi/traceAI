# OpenClaw tracing for Future AGI

Trace local OpenClaw model requests and tools using OpenClaw's official `diagnostics-otel` plugin. This package provides setup, a local launcher, privacy filtering and usage normalization; it does not instrument the model a second time or run a background service.

Requires OpenClaw **2026.10.5 or later in the 2026.x line**, Node **24.16 or newer in the 24.x line**, and macOS or Linux. Use the matching `@traceai/agents` release.

## Quick start

### 1. Get your keys

Create an API key and secret key in [Future AGI → Keys](https://app.futureagi.com/dashboard/keys). Setup asks for both with hidden terminal input; do not paste them into an agent conversation.

### 2. Set up OpenClaw

```bash
npx @traceai/agents setup openclaw --project coding-agents
```

Setup validates the destination and credentials, saves a private configuration, and installs the official diagnostics plugin plus the maintained integration plugin through OpenClaw's own installer. Review the host's source and capability prompts for the lifecycle plugin; setup does not accept them for you. Headless setup returns `TRUST_REQUIRED` until you complete that review interactively. It preserves existing plugin allowlists and other host settings. It does not approve conversation-access hooks, change model providers, or restart a running Gateway.

Content capture is on by default. Captured messages and tool results can include source files or secrets that the agent reads. To capture only structure and usage instead:

```bash
npx @traceai/agents setup openclaw --project coding-agents --no-capture-content
```

### 3. Run a local turn

```bash
npx @traceai/agents run openclaw -- agent --local --agent main \
  --session-id my-review --message "Read README.md and summarize the project."
```

Use your configured agent ID instead of `main` if needed. Reuse `--session-id` for subsequent turns in the same conversation. If omitted, the launcher creates a new session ID for that invocation. Open your project in [Future AGI](https://app.futureagi.com) to inspect the model and tool spans.

**Use the launcher for traced runs.** An ordinary `openclaw` launch is not reconfigured to send traces. `agent exec`, Gateway, channels, TUI and Control UI are not supported by this launcher. It rejects those surfaces rather than silently claiming tracing is enabled.

## What you see

- One prompt trace per launcher invocation, with native model-call and tool spans beneath it. Native timings and internal ancestry are preserved; original trace/span IDs are recorded as provenance.
- Model input/output and tool input/output when exposed by the official plugin and permitted by your privacy settings.
- Request-level input/output/cache usage, reported time to first byte, model/provider and error status.
- An opaque session ID derived from the explicit session ID, and your configured opaque user ID.
- Native run and context spans, including other native lifecycle spans when OpenClaw emits them.

Retries stay beneath the same `openclaw.prompt` root. A subsequent invocation creates a new trace even when you reuse the session ID. The root measures the launcher invocation, not a model call, and does not duplicate request usage. A prompt rejected before native spans arrive still has an outcome root with a `MISSING_NATIVE_SPANS` diagnostic. Original native IDs are available in `fi.agent.native.trace_id`, `fi.agent.native.span_id` and, for native children, `fi.agent.native.parent_span_id`.

The integration removes `openclaw.model.usage` rollups so successful request usage is not counted a second time. Counters are not redistributed to failed attempts. When an external harness exposes only turn-level usage, the prompt root owns that usage once rather than fabricating individual model requests. Cache counters use Future AGI's canonical cache-read and cache-write fields without adding them to input totals again.

OpenClaw 2026.10.5 exports cost estimates as metrics, not model-call trace attributes. Metrics are disabled here, so no cost is invented. Native system prompts and external harness internals are not exposed as model/tool content. The integration does not claim full subagent or compaction coverage beyond spans the official plugin actually emits.

## Configuration

Credentials are stored in `${XDG_CONFIG_HOME:-$HOME/.config}/futureagi/agents/credentials.json` with mode `0600`. OpenClaw preferences are in `futureagi.json` beside its host config. Host configuration discovery respects `OPENCLAW_CONFIG_PATH`, then `OPENCLAW_STATE_DIR`, then `OPENCLAW_HOME/.openclaw`, then `$HOME/.openclaw`, including `~` expansion. No `.env` files, shell profiles, auth caches or keychains are read by this package.

For headless setup, provide `FI_API_KEY` and `FI_SECRET_KEY` as a complete pair in the process environment. The pair overrides the saved credentials. The launcher removes these variables before the model and tool runtime starts; a program running as your OS user can still access your credential store.

| Setting | Effect |
| --- | --- |
| `FI_AGENT_ENABLED=false` | Unconditional export kill switch; the agent still runs. |
| `FI_AGENT_CAPTURE_CONTENT=false` | Exclude content and error messages. |
| `FI_HIDE_INPUTS=true` / `FI_HIDE_OUTPUTS=true` | Preserve stricter input/output privacy. |
| `FI_HIDE_ERROR_DETAILS=true` | Exclude error details while retaining status. |
| `FI_PROJECT_NAME` | Future AGI destination project. |
| `FI_USER_ID` | Explicit user identifier; otherwise use the saved opaque ID. |
| `FI_BASE_URL` | Self-hosted API origin. |
| `FI_OTLP_ENDPOINT` | Exact traces URL; overrides the base URL. |
| `FI_AGENT_CONFIG_DIR` | Override the shared protected configuration directory. |

The Cloud endpoint is `https://api.futureagi.com/tracer/v1/traces`. Exports include `X-Api-Key`, `X-Secret-Key` and `X-Project-Name`, plus resource attributes `project_name` and `project_type=observe`. Metrics and logs are off.

For a project-specific destination, configure and run with the same project directory:

```bash
npx @traceai/agents setup openclaw --scope project --project-dir /path/to/repo --project my-project
npx @traceai/agents run openclaw --project-dir /path/to/repo -- agent --local --agent main --message "Summarize README.md"
```

The repository gets `.futureagi/agents.json`, not credentials. A repository cannot silently raise your content-capture ceiling or send saved credentials to a new destination.

## Status and controls

```bash
npx @traceai/agents status openclaw --check
npx @traceai/agents doctor openclaw
npx @traceai/agents disable openclaw
npx @traceai/agents enable openclaw
npx @traceai/agents uninstall openclaw
```

Status separates plugin registration, observed exports and credential acceptance; it does not infer Cloud rendering from a successful POST. A fresh installation reports activation unobserved until spans are exported. `disable` needs no credentials or network and fences already-running exporters, including queued work. Requests already accepted by a server cannot be recalled.

After upgrading `@traceai/agents`, run `npx @traceai/agents update openclaw`. If the bundled lifecycle plugin version changed, update stages the new version and asks you to review OpenClaw's native uninstall/install prompts. Headless update prints the exact interactive commands and leaves the old installation untouched until you approve replacement. Rerun update after completing them.

Uninstall restores only unchanged, owned diagnostics settings. Shared native plugins remain installed; without the explicit launcher they do not export through Future AGI. Modified settings are not overwritten.

## Privacy and delivery

The in-process normalizer removes host/process metadata, arbitrary attributes, event payloads and tool definitions. It bounds each captured field to 20,000 characters, applies best-effort redaction and removes known Future AGI credentials. This is not a guarantee that arbitrary application secrets are detected; turn content capture off for sensitive workloads.

The official plugin uses its existing `OPENCLAW_OTEL_PRELOADED` SDK handoff inside the launcher's child process. No existing global provider is replaced. Conflicting preloads are rejected; ordinary Node memory and CA options are preserved. No local receiver, daemon, copied interpreter or model proxy is involved.

The launcher owns network delivery so OpenClaw can exit without discarding completed spans. Capture records are encrypted with a per-run, memory-only key and written to an immediately unlinked private file descriptor, capped at 4 MiB per invocation. No plaintext capture file or reusable decryption key is saved. The launcher drains delivery for at most five seconds after the child exits, then closes the descriptor. Capture limits and delivery failures appear in status. Network failure drops telemetry rather than failing a model/tool call. Delivery is standard OTLP, not exactly-once; force-killing the launcher can lose queued spans.

## Troubleshooting

- **Unsupported host version:** use OpenClaw 2026.10.5 or later in the 2026.x line with Node 24.16+ in the 24.x line. Other major version lines are rejected explicitly. Unknown model observation shapes retain a structural span with `UNSUPPORTED_NATIVE_SPAN_SHAPE` instead of inventing content or usage.
- **Project config ignored:** `PROJECT_CONFIG_IGNORED` means the repository config is malformed, shared-writable, or otherwise unsafe. Tracing continues with your protected user settings; move the project to an owner-controlled directory if you need project overrides.
- **Existing exporter or preload:** do not run two tracing routes together. Keep your existing route or remove it explicitly before setting up this one. Setup never disables an active existing native exporter.
- **Registered but no exports:** run through `fi-agent run openclaw -- agent --local ...`, not `agent exec` or an already-running Gateway. Check the selected agent/provider configuration with OpenClaw itself, then run `doctor`.
- **Destination not approved:** run setup again for the intended project and endpoint; do not edit a repository file to redirect saved credentials.
- **Registration modified:** inspect the host config changes. The integration will not overwrite settings that changed after installation.
- **Missing content:** check both Future AGI privacy settings and OpenClaw's own access policy. Enabling diagnostics is not permission to approve conversation-access hooks.
