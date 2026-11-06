# Future AGI for Hermes Agent

Trace each Hermes CLI prompt, its model requests and completed tools in Future AGI. Turns from the same conversation share a session.

## Quick start

1. Get your API key and secret key from [Future AGI Keys](https://app.futureagi.com/dashboard/keys).
2. With Hermes and Node.js 22.16+ or 24 installed, run:

   ```sh
   npx @traceai/agents setup hermes --project coding-agents
   ```

   Enter keys in the masked terminal prompts. Setup validates them, saves a private profile and invokes Hermes' native plugin installer. Review Hermes' Python dependency prompt. Content capture is on, including prompts, answers and tool results; these may contain file contents or secrets the agent reads. Add `--no-capture-content` for metadata only.
3. Start a new Hermes CLI session:

   ```sh
   hermes chat --oneshot -q "Read README.md and summarize this project."
   ```

   Open **Observe → coding-agents** in Future AGI and select the `hermes.turn` trace. Open **Sessions** to follow a conversation across turns.

Node is used only for setup and diagnostics. Tracing runs in the maintained Python plugin inside Hermes: no Node process, daemon, collector or copied hook script is needed at runtime.

## Status and control

```sh
npx @traceai/agents status hermes --check
npx @traceai/agents disable hermes
npx @traceai/agents enable hermes
npx @traceai/agents uninstall hermes
```

Status distinguishes registration, observed activation and HTTP export acceptance; acceptance does not prove product readback. Export counts are the latest process snapshot, not lifetime totals across concurrent processes. Disable fences already queued exports too. Restart Hermes after re-enabling or changing the destination/privacy settings.

The active profile is discovered through `hermes profile`; an explicit `HERMES_HOME` takes precedence. Setup never changes your active profile, provider, model or dependency consent. It does not overwrite an existing plugin. For an older installation, upgrade the Python plugin through Hermes before repeating setup. Uninstall removes only a native installation recorded as owned by this setup.

## Configuration

Credentials are in `${XDG_CONFIG_HOME:-$HOME/.config}/futureagi/agents/credentials.json` (0600). The active Hermes home contains `futureagi.json`, referencing that protected profile. `FI_AGENT_CONFIG_DIR` relocates the shared store. The Python runtime reads the same bounded, version-1 JSON files as the TypeScript CLI; there is no cross-language executable bridge.

- `FI_API_KEY` and `FI_SECRET_KEY`: override saved credentials as a complete pair.
- `FI_PROJECT_NAME`, `FI_USER_ID`: destination project and an explicit opaque user identity.
- `FI_BASE_URL`: self-hosted API origin. `FI_OTLP_ENDPOINT`: exact traces URL, taking precedence.
- `FI_AGENT_CAPTURE_CONTENT=false`: metadata only; `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, and `FI_HIDE_ERROR_DETAILS` retain stricter controls.
- `FI_AGENT_ENABLED=false`: kill switch, independent of credentials/network.
- `FI_HERMES_MAX_CHARS`: per-field content cap, 256–20,000 characters.

For CLI project overrides, run setup with `--scope project --project-dir /absolute/project/path`. Non-secret settings go in `.futureagi/agents.json`, keyed by `hermes`; project-bound credentials remain outside the repository. Launch Hermes from that directory. A project cannot raise the saved privacy ceiling or send saved keys to an unapproved destination. Gateway profile routing is separate; project-directory overrides are CLI-only.

## Native Python-only alternative

If Node is not installed, configure the `FI_API_KEY`/`FI_SECRET_KEY` environment pair using your secret manager and install directly:

```sh
hermes plugins install future-agi/traceAI/python/frameworks/hermes-agent/hermes-plugin/futureagi --enable
```

This path uses `traceAI-hermes-agent` 0.1.1+ and remains independent of npm. Env-only installations retain their earlier metadata-only default; set `FI_AGENT_CAPTURE_CONTENT=true` to opt in. Native diagnostics are `hermes plugins list --json`. Shared setup provides the validated credential store and richer status; it is not a required runtime dependency.

## Trace fidelity and privacy

`pre_api_request`/`post_api_request`/`api_request_error` define model attempts. Turn summaries are not additional generations and do not duplicate their token usage. Tools use actual call IDs and durations. In-process delegated turns nest under their parent turn where Hermes provides parent IDs; arbitrary subprocess ancestry is not inferred. Compaction and approval observers add events to an existing turn on hosts that expose them. Missing cost and auxiliary model calls are not fabricated.

Content is bounded and configured FI keys are redacted best-effort; this is not a general secret/PII scrubber. System prompts are represented by size rather than text. No transcripts, memory, model credentials, `.env` files or keychains are read by the plugin. It uses a private OpenTelemetry provider and a bounded in-memory queue. Failed exports are dropped after bounded attempts, without a persistent content journal or exactly-once delivery guarantee. Export errors cannot change Hermes' result.

Current Hermes CLI clients can reuse a persistent native gateway. After changing environment variables or saved destination/privacy configuration, restart that gateway when idle (`hermes gateway restart`); a new CLI window alone is not a restart. Environment switches must be set in the gateway launch environment. For immediate disable of an already-running gateway, use the shared `disable hermes` command. Shared setup does not start an additional Future AGI daemon.
