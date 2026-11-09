# Oh My Pi tracing

See each Oh My Pi prompt, model generation and tool call in Future AGI, grouped by conversation.

Requires Oh My Pi 18.x (18.17.0 or later) and Bun 1.4.4 or later. The unified setup CLI requires Node.js 22.16+ (22.x) or 24.x. OMP must already have a working model configuration; Future AGI does not supply a model key.

## Quick start

1. Get an API key and secret key from [Future AGI keys](https://app.futureagi.com/dashboard/keys).
2. Run setup and enter the keys at the masked prompts:

   ```bash
   npx @traceai/agents setup oh-my-pi --project coding-agents
   ```

3. Restart Oh My Pi and send a prompt:

   ```bash
   omp --print "Read README.md and summarize this project."
   ```

Open **Observe** in Future AGI and select `coding-agents`. Each dequeued user prompt produces a separate trace. Model and tool continuations stay inside that trace; conversation turns share a session.

If a Future AGI profile is already configured, you can install through Oh My Pi directly:

```bash
omp install npm:@traceai/oh-my-pi-plugin
```

The native npm plugin is user-scoped. You do not copy hooks into your project and no separate background worker is needed.

## Configuration

Setup validates the destination, saves credentials in an owner-only (`0600`) profile and installs the native plugin. Environment variables override saved settings.

| Setting                          | Purpose                                                                    |
| -------------------------------- | -------------------------------------------------------------------------- |
| `FI_API_KEY`, `FI_SECRET_KEY`    | Credential pair; supply both together                                      |
| `FI_PROJECT_NAME`                | Destination project                                                        |
| `FI_BASE_URL`                    | Self-hosted API origin                                                     |
| `FI_OTLP_ENDPOINT`               | Full OTLP/HTTP traces URL                                                  |
| `FI_AGENT_CAPTURE_CONTENT=false` | Export structure, timing and usage without content                         |
| `FI_AGENT_ENABLED=false`         | Disable collection and export                                              |
| `FI_USER_ID`                     | Explicit user identity; otherwise the profile uses an opaque local user ID |

For a project-specific destination, run setup from that project:

```bash
npx @traceai/agents setup oh-my-pi --project my-project --scope project
```

The tracing override is project-specific; OMP's npm installation remains user-scoped. Project settings cannot redirect saved credentials to an unapproved endpoint.

```bash
npx @traceai/agents status oh-my-pi --check
npx @traceai/agents disable oh-my-pi
npx @traceai/agents enable oh-my-pi
npx @traceai/agents uninstall oh-my-pi
```

`status` reports configuration, credential connectivity, native registration and the last exports. Setup's registration result does not mean an agent session has already loaded the extension: restart OMP and send a prompt first.

## Trace details

- One trace per dequeued user work item, including print-mode prompts; model/tool iterations are not separate prompt traces.
- Actual primary and task assistant messages supply **reconstructed generations**, with the prompt and bounded observed context where available. Tool-call-only messages include their tool calls. These are not full provider requests or measured wire durations.
- OMP's provider callback does not identify the request or distinguish advisors from the primary agent. Advisor and other auxiliary generations, including their usage, are therefore not exported. The root records `AUXILIARY_GENERATIONS_UNAVAILABLE`; this coverage gap does not turn a successful prompt into an error. Hidden retry attempts are not invented.
- OMP supplies input/output tokens, cache counts and price-table cost estimates. Cost is not a provider invoice.
- Tool inputs, outputs and errors, plus compaction events occurring during the prompt.
- OMP's task lifecycle forwards child generations and tools under the spawning tool, including nested tasks and background completion. A child without a verifiable parent tool ID is not guessed into another trace.

## Privacy and troubleshooting

Prompts, model outputs and tool input/output are captured by default. Use `--no-capture-content` during setup or `FI_AGENT_CAPTURE_CONTENT=false` for metadata-only tracing. Content is bounded and common secrets are redacted; redaction cannot guarantee removal of every secret. Images, binary payloads, transport headers and thinking signatures are not exported.

Remove an old `futureagi-otlp.ts` copied hook before installing this plugin. Do not also send OMP's native OTLP traces to Future AGI for the same session: that creates duplicates. `OTEL_TRACES_EXPORTER=none` disables OMP's own trace exporter without disabling this plugin.

If no traces arrive, check `status --check`, confirm the plugin is enabled in `omp plugin list`, and restart OMP. A project-level plugin override or an OMP launch with extension discovery disabled can prevent loading. Network failures are bounded and fail open: OMP continues, and unexported spans may be dropped rather than persisted with prompt content.

If `status --check` or setup reports `AUTH_REJECTED`, replace revoked keys through masked prompts:

```bash
npx @traceai/agents setup oh-my-pi --replace-credentials
```

For a project-scoped profile, run this from that project with `--scope project`. Unset any old `FI_API_KEY` and `FI_SECRET_KEY` environment overrides first: environment credentials take precedence over masked entry. In automation, inject both replacements through your secret manager and rerun setup; do not put keys in command arguments. Replacement keys are validated before the saved profile is overwritten. Restart OMP after replacing credentials.

`PROJECT_CONFIG_IGNORED` means a project configuration could not be safely read (for example on a shared external volume). Tracing continues with the user profile, ignoring that project override. Do not loosen credential-file permissions. For a sensitive project, use `FI_AGENT_CAPTURE_CONTENT=false` in the launch environment or disable tracing rather than relying on an ignored project override.

Uninstall removes only an unchanged registration created by setup. If you installed or changed the package manually, use `omp plugin uninstall @traceai/oh-my-pi-plugin` yourself.
