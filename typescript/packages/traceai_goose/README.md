# Future AGI for Goose

Trace Goose CLI conversations without running a collector or copying instrumentation scripts.

## Quick start

1. Get your API key and secret key from <https://app.futureagi.com/dashboard/keys>.
2. Run `npx @traceai/agents setup goose --project coding-agents` and enter the keys at the hidden prompts.
3. Run `goose run -t "List the files in this folder"`, then open the `coding-agents` project in Future AGI's Tracing view.

Requires Goose CLI **1.54+ in the 1.x line**, Node **22.16+ (22.x)** or **24.x**, and Linux or macOS. Goose's normal provider configuration is unchanged. Setup validates the destination before persisting credentials, uses owner-only files, and installs a bundled runtime in the user's configuration directory. Goose discovers the native plugin at `~/.agents/plugins/futureagi-goose` (or under `GOOSE_PATH_ROOT`). It does not depend on the npx cache remaining available.

**Content capture is on by default.** Prompts, responses, and tool bodies may contain file contents or secrets. Use `setup --no-capture-content` or `FI_AGENT_CAPTURE_CONTENT=false` for structure and usage only. Best-effort redaction is not a guarantee; each content field is capped at 20,000 characters.

## Controls

```sh
npx @traceai/agents status goose --check
npx @traceai/agents disable goose
npx @traceai/agents enable goose
npx @traceai/agents uninstall goose
```

`FI_AGENT_ENABLED=false` disables collection and export, including hooks already running. For a project-only installation, run `npx @traceai/agents setup goose --scope project --project my-project` from that project's root. The globally discovered hook only collects in explicitly registered canonical project directories unless you also perform user-scoped Goose setup. Another agent's saved credentials do not enable Goose. Status, doctor, enable and update use the current directory; `--project-dir` selects a different root. Environment settings override saved settings; repositories cannot redirect saved credentials, broaden the installed scope or elevate content permissions. `FI_BASE_URL` supports a self-hosted origin; setup accepts `--base-url` to validate and persist it. `FI_USER_ID` overrides the generated pseudonymous user ID.

## Captured data

Native `UserPromptSubmit`, tool, `Stop`, and `SessionEnd` hooks establish turn and tool boundaries. A `Stop` attempt can be rejected by another policy hook, so export waits for session exit or the next prompt. A completed `goose run` exports before exiting; in an interactive session, submit another prompt or exit normally to export the preceding turn. The runtime reads current-turn content and bounded logical-message identity metadata from Goose's saved session database, read-only. It exports through the shared `@traceai/agent-core` client, never through a model gateway, MCP extension, or receiver daemon.

- One trace per prompt, one pseudonymous session per conversation, user attribution.
- Generation records with provider/model, input/output tokens, cache usage, and source costs when recorded. Estimated costs remain labeled as estimated.
- Tools beneath the generation that requested them, with input/output and error status.
- Compaction markers when the saved provider usage explicitly identifies a compaction.
- No OS-user/host identity, native log-line span events, logs, or metrics. No duplicate session/agent usage totals.

Generation fidelity is `recorded-request`: these are persisted provider-response records, **not** a guessed LLM span derived from final assistant text. Generation placement uses second-resolution message timestamps; tool duration uses hook intervals. Generation input is the prompt or preceding tool result, not the full provider context. Native OTLP span IDs and retry attempts are not available in the saved records. Cross-session subagent nesting, Goose Desktop, and `--no-session` runs are not supported by this adapter.

Capture is bounded to 256 messages / 4 MiB per turn, 4,096 message identities per conversation, 48 generation/tool records, 128 tool timings and 512 session cursors. State contains no conversation text and expires after 24 hours. Duplicate terminal hooks do not retry exports: delivery is best effort, not exactly once. The hook exits within an 8-second capture budget (10-second host timeout) even when the endpoint stalls, and never emits a blocking decision. Completed turn duration ends at the confirmed Stop, not at later delivery. Metadata includes the canonical project directory so a resumed session exports to its original project even when launched elsewhere.

## Migrating native OTel

The plugin refuses to run with an active native Goose trace exporter. Direct native export would bypass the identity filtering and usage de-duplication in the older collector recipe. Turn that exporter off before setup:

```sh
export OTEL_TRACES_EXPORTER=none
export OTEL_METRICS_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
npx @traceai/agents setup goose --project coding-agents
```

Keep these settings in the shell that starts Goose, or remove the old native OTel endpoint/configuration. No native destination or provider config is silently changed. After migration the plugin replaces the collector's privacy/accounting behavior by constructing only the supported attributes and assigning billable usage only to provider-response records.

## Troubleshooting

- **No traces:** for your first trace, finish a `goose run -t "List the files in this folder"` task. In an interactive session, submit another prompt or exit normally before looking for the preceding turn. If it is still missing, start a new Goose process; check `status goose --check` and Goose's `disabledPlugins` list.
- **Runtime unavailable:** run `setup goose` again using an installed, supported Node interpreter. Setup pins your existing interpreter, preferring its stable PATH symlink; it never copies Node.
- **Project configuration ignored:** move the project configuration to a safely owned directory or use user-scoped setup. Tracing continues with the user configuration when a project layer has unsafe permissions; status and doctor report the ignored layer.
- **Native tracing conflict:** disable the old native exporter as above. Do not send native Goose OTLP directly to Cloud.
- **Capture limit:** split an oversized task into smaller prompts; status records the bounded diagnostic code.
- **Provider failure before a saved response:** a turn can be incomplete; the plugin does not invent request usage or an error message.
- **Connection failure:** Goose continues. Check the destination and keys, then run another prompt; ambiguous exports are never replayed.

The adapter accepts stable Goose 1.x releases from 1.54 and validates the saved-message shape before export. An incompatible schema is reported in status instead of being interpreted as valid usage. Major-version changes require an integration update.
