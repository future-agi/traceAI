# traceAI-hermes-agent

The maintained Python observer plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/). Each top-level user turn becomes a Future AGI trace, with model-request attempts and completed tools beneath it. Turns from one conversation share `session.id`.

## Quick start

1. Get API and secret keys from [Future AGI Keys](https://app.futureagi.com/dashboard/keys).
2. Run the shared setup command in a terminal with Hermes installed:

   ```sh
   npx @traceai/agents setup hermes --project coding-agents
   ```

   Setup validates the keys, saves a 0600 credential profile, discovers the active Hermes home and invokes its native plugin installer. Review Hermes' Python dependency prompt. **Content capture is on**: prompts, model output and tool results can include file contents or secrets the agent reads. Add `--no-capture-content` to keep metadata only.
3. Start a new session:

   ```sh
   hermes chat --oneshot -q "Read README.md and summarize this project."
   ```

   Open **Observe → coding-agents** in Future AGI and inspect `hermes.turn`. Open **Sessions** to follow multiple turns in a conversation.

Node is needed only for shared setup/status, not for the Python tracing runtime. No collector, daemon or copied hook script is required.

## Python-only installation

For environments without Node, configure the `FI_API_KEY`/`FI_SECRET_KEY` pair through your secret manager and use Hermes' native installer:

```sh
hermes plugins install future-agi/traceAI/python/frameworks/hermes-agent/hermes-plugin/futureagi --enable
```

For a Python environment you manage, install `traceAI-hermes-agent` in the same environment as Hermes, then run `hermes plugins enable futureagi`. Hermes discovers the `futureagi` entry point. The plugin is enabled per Hermes home; use your intended `HERMES_HOME` or active profile.

Existing env-only installations retain their metadata-only default. Set `FI_AGENT_CAPTURE_CONTENT=true` to capture content. `FI_HERMES_CAPTURE_CONTENT` remains a compatibility alias, with the shared variable taking precedence. Shared setup explicitly saves the chosen content policy rather than silently changing existing installations.

## Configuration and control

Shared setup stores keys in `${XDG_CONFIG_HOME:-$HOME/.config}/futureagi/agents/credentials.json` (0600) and a non-secret reference in the active Hermes home's `futureagi.json`. The runtime reads these bounded version-1 JSON files directly; it never imports TypeScript or starts Node. `FI_AGENT_CONFIG_DIR` overrides the shared directory.

```sh
npx @traceai/agents status hermes --check
npx @traceai/agents disable hermes
npx @traceai/agents enable hermes
```

Status separates registration, observed activation and HTTP export acceptance. Counters are the most recent process snapshot, not durable lifetime totals. Disable also fences queued exports. Restart Hermes after enabling or changing destination/privacy configuration.

| Setting | Behavior |
| --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Override saved credentials as a complete pair; a single key never mixes with a saved one. |
| `FI_PROJECT_NAME` | Destination project; Python-only default is `hermes-agent`. |
| `FI_BASE_URL` | API origin, default `https://api.futureagi.com`; appends `/tracer/v1/traces`. |
| `FI_OTLP_ENDPOINT` | Exact traces URL; wins over base URL. |
| `FI_USER_ID` | Explicit user identity; shared setup otherwise generates an opaque ID. |
| `FI_AGENT_ENABLED=false` | Kill switch. |
| `FI_AGENT_CAPTURE_CONTENT=false` | Exclude prompts, model/tool content and error details. |
| `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_ERROR_DETAILS` | Preserve stricter privacy controls. |
| `FI_HERMES_MAX_CHARS` | Per-field cap, default 20,000 characters, bounded to 256–20,000. |

CLI project overrides use `setup --scope project --project-dir /absolute/path` and `.futureagi/agents.json` keyed by `hermes`. Launch from that directory. The repository contains no credentials; destination-approved project profiles stay in the private store. A repository cannot raise the user-approved privacy ceiling or reroute saved credentials.

A gateway has separate profile scopes. The plugin uses Hermes' scoped setting accessor and resolved home, never another profile's saved credentials. CLI project-directory routing is not applied to gateway events. Gateway callbacks are not a claim of messaging-platform end-to-end support.

## Trace fidelity

| Observer | Result |
| --- | --- |
| `pre_llm_call`, `post_llm_call`, `on_session_end` | One `hermes.turn` AGENT span with prompt, answer and outcome; no duplicated aggregate tokens. |
| `pre_api_request`, `post_api_request`, `api_request_error` | LLM span for each actual request/attempt, model/provider, request ID, available tokens/cache/reasoning, finish reason, time to first chunk and errors. |
| `post_tool_call` | TOOL span with actual call ID, duration, arguments/result and status. |
| `subagent_start`, `subagent_stop` | In-process child turns beneath the parent turn when Hermes supplies parent IDs. |
| `post_compaction`, `post_approval_response` | Events on existing turns, when supported by the host. Never a new prompt/generation. |
| `on_session_finalize`, process exit | Close unfinished spans and perform bounded export. |

No policy hooks are registered and no directives are returned. In particular, the plugin avoids `pre_tool_call`, which Hermes treats as fail-closed. A tool without a completed callback has no tool span. Arbitrary subprocess ancestry, cost, auxiliary model calls, streaming deltas, and gateway delivery/cron events are not synthesized.

## Privacy and delivery

The plugin observes event payloads, not transcripts, memory, skills, `.env` files or keychains. Content-bearing fields are bounded, configured FI keys are redacted best-effort, and system prompts are represented by size only. Redaction is not a general secret/PII scrubber. Model IDs, provider hostnames, tool names and source IDs are metadata and may themselves be sensitive.

A private OpenTelemetry provider leaves the host's global provider and signal handlers alone. Exports use standard OTLP/HTTP with explicit API, secret and project headers; redirects are refused. Batching and shutdown are bounded. Delivery failures drop data rather than block the agent indefinitely; there is no disk content queue or exactly-once guarantee.

## Troubleshooting

- **Not registered:** run `hermes plugins list --json` in the intended profile. Retry `hermes plugins enable futureagi` to complete dependency consent. Existing older plugins must be updated through Hermes before using shared configuration.
- **Not activated:** start a new CLI session after installation, then check status again.
- **No exports:** check destination, complete key pair, privacy/enablement settings and `status hermes --check`. An HTTP-accepted export is distinct from a rendered trace in Observe.
- **Slow exit:** a collector outage can delay final delivery by a few seconds. The plugin drops undeliverable spans.
- **Unfinished or interrupted turns:** Hermes stopped before a normal terminal callback. They are not reported as successful completions.

Current Hermes CLI clients can reuse a persistent native gateway. After changing environment variables or saved destination/privacy configuration, restart that gateway when idle (`hermes gateway restart`); a new CLI window alone is not a restart. Environment switches must be set in the gateway launch environment. For immediate disable of an already-running gateway, use the shared `disable hermes` command. Shared setup does not start an additional Future AGI daemon.
