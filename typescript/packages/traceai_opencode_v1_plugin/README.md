# OpenCode V1 tracing

Trace each OpenCode prompt in Future AGI, with model steps and tools nested below it and conversations grouped into sessions.

Requires OpenCode **V1, 1.19.0 or newer**, on Linux, and Node.js 22.16+ (22.x) or 24.x for setup. This is the V1 hooks integration, not the OpenCode V2 plugin API. Remove a previously copied `futureagi-otlp` observer before installing this plugin to avoid duplicate traces.

## Quick start

### 1. Get your keys

Create an API key and secret key on the [Future AGI keys page](https://app.futureagi.com/dashboard/keys).

### 2. Set up tracing

```bash
npx @traceai/agents setup opencode-v1 --project coding-agents
```

Enter your keys in the masked terminal prompts. Setup checks the destination, stores credentials in an owner-only file, and adds `@traceai/opencode-v1-plugin@0.1.0` to OpenCode's `plugin` array without replacing other settings. Restart OpenCode; its native plugin loader installs the package.

Content capture is on by default. Prompts, assistant output and tool input/output may include file text and secrets the agent reads. To keep only structure and usage, add `--no-capture-content` to setup.

### 3. Use OpenCode

```bash
opencode run "Read README.md and summarize this project."
```

Open **Observe** in [Future AGI](https://app.futureagi.com), select `coding-agents`, and inspect the latest turn. The **Sessions** view groups prompts from the same conversation, including resumed conversations. Each turn exports once after OpenCode goes idle and its final model/tool updates arrive; the final turn does not need another prompt to appear.

## Configuration

Environment variables override saved configuration. Supply `FI_API_KEY` and `FI_SECRET_KEY` together for headless setup; do not put credentials in command arguments or project files.

| Setting | Purpose |
| --- | --- |
| `FI_PROJECT_NAME` | Future AGI destination project |
| `FI_USER_ID` | Explicit user identifier; otherwise a local opaque identity is used |
| `FI_AGENT_CAPTURE_CONTENT=false` | Omit prompt, model context, assistant text and tool bodies |
| `FI_AGENT_ENABLED=false` | Disable collection and export |
| `FI_BASE_URL` | Self-hosted API origin, such as `https://api.example.com` |
| `FI_OTLP_ENDPOINT` | Exact OTLP traces URL; overrides `FI_BASE_URL` |
| `FI_AGENT_CONFIG_DIR` | Location of the protected shared tracing configuration |

The default credential file is `${XDG_CONFIG_HOME:-$HOME/.config}/futureagi/agents/credentials.json`. OpenCode-specific settings are in `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/futureagi.json`. Files containing credentials are mode `0600` with private parent directories.

For a project override, use:

```bash
npx @traceai/agents setup opencode-v1 --project my-project \
  --scope project --project-dir /absolute/path/to/project
```

The repository stores no keys. A project cannot raise a saved privacy ceiling or silently send saved or environment-supplied credentials to another destination or project. Destination changes require setup approval.

## Status and removal

```bash
npx @traceai/agents status opencode-v1 --check --json
npx @traceai/agents disable opencode-v1
npx @traceai/agents uninstall opencode-v1
```

Status separates registration, observed activation, credential acceptance and the last accepted export for the current destination, project and installation. After setup or re-enabling tracing, submit a new prompt to observe activation. An accepted export does not by itself prove product readback. Disable fences an already-running instance and its queued exports. Uninstall removes only the plugin entry owned by setup and preserves unrelated configuration.

## What is captured

- One immutable root per submitted user prompt; no repeatedly updated session roots.
- Actual assistant model steps, with available model/provider identifiers, token counts, cached/reasoning token details and host-calculated cost estimates. Usage belongs to model steps only, not the turn root as well.
- The bounded message context exposed by OpenCode before a step, and assistant text. This is not a byte-for-byte provider request: system prompts, tool schemas and provider transformations are not included.
- Tool input/output, timing and failures from message parts. Task child sessions are nested only when OpenCode provides an explicit spawning-tool session ID.
- Compaction activity inside the current turn, and interrupted/incomplete outcomes rather than successful completion on shutdown.

Content is bounded to 20,000 characters per field. Known Future AGI keys and recognizable secret patterns are redacted on a best-effort basis; redaction is not a guarantee that arbitrary secrets are removed. Stricter `FI_HIDE_*` settings remain effective.

Export is asynchronous, bounded and memory-only. Telemetry failures do not change agent responses, tool decisions or exit codes. A forced process kill can lose pending traces. Delivery is not exactly once; interrupted or rejected batches are not blindly retried.

## Troubleshooting

**No traces:** Run `status opencode-v1 --check --json`. Check `enabled`, `registration`, `activation` and delivery errors separately. Restart OpenCode after setup, and submit a new prompt; historical sessions are not imported.

**Unsupported version:** Use OpenCode V1 version 1.19.0 or newer (below 2.0.0) for this integration. Do not install the V1 and V2 tracing packages together.

**Duplicate registration:** Remove an older copied Future AGI observer or duplicate project/user plugin entry. Setup does not silently delete unrelated plugins or approve project trust.

**Installation failed:** Check file ownership, symlinks and OpenCode configuration syntax. Existing JSONC comments are preserved. Inline `OPENCODE_CONFIG_CONTENT` is not supported by setup; use an OpenCode configuration file instead.

**Unsafe project configuration:** If status reports `PROJECT_CONFIG_IGNORED_UNSAFE`, tracing uses your protected user configuration instead. Check project-directory ownership and permissions before using project-scoped setup; do not loosen credential-file permissions.

**No model content:** Check `FI_AGENT_CAPTURE_CONTENT` and any `FI_HIDE_*` settings. Usage and tool structure still appear with content capture disabled.
