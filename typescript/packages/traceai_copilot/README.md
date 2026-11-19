# GitHub Copilot CLI tracing

See each Copilot interaction, model call, and tool call in Future AGI, grouped by conversation.

Requires Node.js 22.16+ (22.x) or 24.x, a POSIX system with `mkfifo`, and GitHub Copilot CLI **1.x** on your `PATH` with the native span schema used by 1.0.106. Copilot's normal login or BYOK model configuration stays unchanged. Your organization's Copilot policies still apply.

## Quick start

### 1. Get your keys

Create an API key and secret key on the [Future AGI keys page](https://app.futureagi.com/dashboard/keys). Setup prompts for both using hidden terminal entry; do not put them in a prompt or command-line argument.

### 2. Set up tracing

```bash
npx @traceai/agents setup copilot --project coding-agents
```

Setup checks the destination using an empty authenticated OTLP request and writes owner-only configuration files. It does not edit shell profiles, Copilot permissions, provider settings, or repository hooks.

Content capture is on by default. Prompts, responses and tool input/output can include file contents and secrets the agent reads. To capture only structure and usage instead:

```bash
npx @traceai/agents setup copilot --project coding-agents --no-capture-content
```

### 3. Run Copilot and inspect the trace

```bash
npx @traceai/agents run copilot
```

Ask Copilot to list the project's top-level files and summarize its purpose. Keep its normal permission prompts and approve only the tool access you intend. Pass Copilot arguments after `--` when running a non-interactive session.

Open [Future AGI](https://app.futureagi.com), choose the `coding-agents` project in Observe, and expand an `invoke_agent` trace to see its `chat` and `execute_tool` children. Conversations share `session.id`; configured user IDs are attached to spans and the resource uses `project_type=observe`.

Start traced sessions through `run copilot`; launching plain `copilot` does not activate this integration. If you install the CLI globally with `npm install -g @traceai/agents`, `fi-agent` is the shorter equivalent of `npx @traceai/agents`.

## Configuration

| Setting                                        | Behavior                                                                                                                 |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `FI_API_KEY` and `FI_SECRET_KEY`               | A complete environment pair overrides saved credentials. Never pass keys as CLI arguments.                               |
| `FI_PROJECT_NAME`                              | Selects the destination project when used with explicitly supplied credentials.                                          |
| `FI_AGENT_CAPTURE_CONTENT=false`               | Turns off content capture for this process.                                                                              |
| `FI_AGENT_ENABLED=false`                       | Disables Future AGI export for this process.                                                                             |
| `FI_HIDE_INPUTS=true` / `FI_HIDE_OUTPUTS=true` | Suppresses input or output content.                                                                                      |
| `FI_HIDE_ERROR_DETAILS=true`                   | Suppresses error detail and error outputs.                                                                               |
| `FI_USER_ID`                                   | Sets the user identifier; otherwise setup generates an identifier.                                                       |
| `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`    | Uses protobuf for Future AGI exports; the default is `http/json`. `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` takes precedence. |

Use an approved self-hosted origin with `setup --base-url https://your-futureagi.example`. The launcher uses the validated Future AGI destination, not inherited `OTEL_EXPORTER_OTLP_*` endpoints or headers.

For a project-specific profile, run setup inside the project:

```bash
npx @traceai/agents setup copilot --scope project --project my-project
npx @traceai/agents run copilot
```

The project gets `.copilot/futureagi.json`; credentials remain in the user-only credential store. Run from that directory, or use `run copilot --project-dir /path/to/project`. Use this option instead of passing Copilot's `-C` flag, so configuration and the working directory agree. Repeating project-scoped setup preserves its profile, destination, project and content preference unless you explicitly change them. A repository cannot silently redirect saved or environment-supplied credentials to an unapproved destination or enable content above your saved privacy ceiling.

## Status and controls

```bash
npx @traceai/agents status copilot --check
npx @traceai/agents disable copilot
npx @traceai/agents enable copilot
npx @traceai/agents uninstall copilot
```

Status shows the resolved destination and privacy policy, CLI version, launcher registration, and accepted/dropped export counts. `--check` makes a fresh connection check. An accepted OTLP response is transport acceptance, not confirmation that a trace is visible in the UI. Uninstall disables tracing and removes the launch registration; it preserves shared credentials and leaves Copilot settings alone.

Disabling fences queued spans from running launchers; re-enabling does not revive their old queues. Start a new session to resume collection.

## Privacy and behavior

Copilot creates the native spans. The foreground launcher consumes them through a private, owner-only named pipe, preserves their trace/span IDs and parent relationships, applies the Future AGI privacy policy, and exports directly. There is no daemon, local HTTP collector, model gateway, or copied hook script. No raw telemetry file is written to disk.

The launcher removes Future AGI credentials and inherited OTLP headers from the Copilot environment and uses Copilot's child-environment secret exclusion. This is not a security sandbox: Copilot runs as your user, and tools you authorize can still read files accessible to that user. Its own session history and logs are controlled by Copilot, not this package.

When output or error-detail hiding is enabled, model-input history keeps only recognized user/system text. Tool responses and prior assistant messages are omitted so hidden outputs cannot reappear in later inputs; unrecognized history is omitted rather than guessed.

Captured text is limited to 20,000 characters per field and known Future AGI credentials are redacted on a best-effort basis. Unknown native attributes are dropped. Export uses a bounded memory queue, no durable retry spool, a maximum five-second request deadline, and a five-second total shutdown export budget. Telemetry failures do not change Copilot's exit status; oversized records or a saturated queue can be dropped and are reported by status. No exactly-once or crash-recovery guarantee is made.

Token/cache/reasoning counts, timing, tool errors, native subagent ancestry and compaction markers are preserved when Copilot emits them. No inferred model calls or estimated costs are added. Only model requests retain additive usage counters. Parent aggregate counts and cost values use the noncanonical `fi.agent.aggregate.*` diagnostic namespace to avoid double counting; native billing multipliers are not labeled as USD.

This package is for the local **Copilot CLI**, not VS Code Copilot Chat or GitHub's hosted coding agent.

## Troubleshooting

- **Setup reports an unsupported host:** use a stable Copilot CLI 1.x release. The launcher disables auto-update for each invocation and validates each native record. If status reports `NATIVE_RECORD_REJECTED` after an update, update the tracing package or use Copilot CLI 1.0.106.
- **Status reports `PROJECT_CONFIG_UNSAFE`:** a project config or its ancestor directory is unsafe or unreadable, so tracing uses your protected user profile instead. This can occur on shared external volumes. Use `FI_AGENT_CAPTURE_CONTENT=false` or `FI_AGENT_ENABLED=false` for an explicit process-level opt-out; repair the directory permissions before relying on project-specific settings.
- **No spans appear:** start through `run copilot`, check `status copilot --check`, and remove `COPILOT_OFFLINE=true` when you want telemetry. Copilot offline mode disables native telemetry even with a local model.
- **Export is rejected:** run `npx @traceai/agents setup copilot --replace-credentials` for hidden replacement-key entry, retaining `--scope project` for a project profile. Headless runs must supply a new `FI_API_KEY` + `FI_SECRET_KEY` environment pair. The previous profile is preserved until replacement keys validate. The agent keeps running even when tracing fails.
- **BYOK does not work:** check Copilot's provider settings and your organization's policy. This integration does not supply model access or bypass policy.
- **A destination change needs approval:** rerun setup for that project/destination. An untrusted project file cannot authorize reusing saved keys elsewhere.
- **A field is missing:** content may be disabled, hidden by a privacy setting, absent from the native host, or over the ingress limit. Do not enable content just to obtain token counts; those are available in structure-only mode.
