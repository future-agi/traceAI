# Claude Code tracing

See each Claude Code prompt, its model generations, tools and subagents in Future AGI, grouped by conversation.

Requires Claude Code **2.x** and Node.js **22.16–22.x or 24.x**. The installer uses Claude Code's user hooks and its own transcripts. Compatible 2.x updates are accepted after transcript-shape checks; unrecognized records produce an incomplete-turn diagnostic rather than silently dropping the turn. It does not require a collector, daemon, model gateway, or beta telemetry flags.

## 1. Get your keys

Create an API key and secret key at [Future AGI keys](https://app.futureagi.com/dashboard/keys). Keep them out of prompts, command arguments and project files.

## 2. Set up tracing

```bash
npx @traceai/agents setup claude --project coding-agents
```

Enter your keys in the hidden terminal prompts. Setup validates the destination, saves the credentials in an owner-only configuration file, and installs the packaged hooks. Existing unrelated hooks and settings are retained. Restart an already-open Claude Code session after setup.

Content capture is **on by default**. Prompts, model responses and tool content can include file text and secrets the agent reads. To keep only structure, timing and token usage instead:

```bash
npx @traceai/agents setup claude --project coding-agents --no-capture-content
```

For noninteractive setup, supply `FI_API_KEY` and `FI_SECRET_KEY` through your secret manager's environment injection. The CLI never accepts secret command-line flags.

## 3. Use Claude Code

```bash
claude
```

Ask Claude to read a small file in your project. In Future AGI, open **Observe**, select **coding-agents**, and open the resulting trace. The **Sessions** view groups prompts from the same conversation.

## Configuration

The integration respects `CLAUDE_CONFIG_DIR`. Its user configuration is `futureagi.json` inside that directory, normally `~/.claude/futureagi.json`. Credentials are stored separately at `${XDG_CONFIG_HOME:-$HOME/.config}/futureagi/agents/credentials.json`, with owner-only permissions.

| Setting                          | Use                                                                            |
| -------------------------------- | ------------------------------------------------------------------------------ |
| `FI_AGENT_CAPTURE_CONTENT=false` | Disable prompt, response and tool content for the process.                     |
| `FI_AGENT_ENABLED=false`         | Disable tracing for the process.                                               |
| `FI_USER_ID`                     | Set an explicit user identifier. Otherwise an opaque local identifier is used. |
| `FI_OTLP_ENDPOINT`               | Set an exact OTLP/HTTP traces URL.                                             |
| `FI_BASE_URL`                    | Set the API origin for self-hosted Future AGI.                                 |
| `FI_AGENT_CONFIG_DIR`            | Relocate the protected Future AGI configuration directory.                     |

Environment settings override the integration's files. A destination change using saved credentials requires setup approval; a repository cannot redirect saved keys to another endpoint. Existing `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and `FI_HIDE_ERROR_DETAILS` restrictions are preserved.

To configure a separate Future AGI project for one repository:

```bash
npx @traceai/agents setup claude --project my-project --scope project --project-dir /path/to/repository
```

The repository's `.claude/futureagi.json` contains no credentials. It can reduce content capture, but cannot raise the user-approved privacy setting. Hooks stay user-scoped. Launching from a subdirectory uses the nearest enclosing configuration, stopping at a repository boundary. Repeating setup preserves the approved project destination and content opt-out unless you explicitly change them.

## Status and lifecycle

```bash
npx @traceai/agents status claude --json
npx @traceai/agents doctor claude
npx @traceai/agents disable claude
npx @traceai/agents update claude
npx @traceai/agents uninstall claude
```

Status and doctor use the current directory's repository configuration, just like the hooks. Use `--project-dir /path/to/repository` to inspect another repository, or `--user-only` to inspect only user configuration.

Status distinguishes installation, observed activation and HTTP export acceptance. HTTP acceptance is not a claim that a trace has been read back from the product. `disable` works offline and prevents queued work from being sent. Uninstall removes only this integration's owned hook entries.

## What the trace contains

- One turn trace for each user prompt, with a stable conversation identifier.
- Generations reconstructed from actual assistant messages and request identifiers. Streamed fragments share usage once, rather than duplicating token counts.
- Available input, output, cache-read and cache-write token usage. Costs are not reported as Anthropic billing.
- Tool input, output, failure status and observed hook timing.
- Nested subagent work correlated through the Agent tool's source agent identifier. Background completion notifications remain in the originating user turn.
- Compaction events observed during a turn.

Generation input is bounded, visible transcript history—not a full API request or hidden system prompt. Generation timing is reconstructed from message timestamps, not measured API latency. A provider failure with no actual assistant generation is an error on the turn, not an invented model span.

## Privacy and delivery

Content fields are limited to 20,000 characters and receive best-effort credential redaction. Redaction is not a guarantee that arbitrary secrets have been removed; use content-off mode when sensitive material must not leave the machine. Thinking blocks, images, native account identifiers and unknown transcript metadata are not exported.

The integration reads only host-supplied transcript paths within Claude Code's project storage. Its own cross-hook files contain metadata and checkpoints, not a second copy of conversation content. They are owner-only and bounded by conversation count and age. Claude Code controls retention of its original transcripts.

Each Stop exports new generations and tools without repeating previously attempted usage. Because another Stop hook can make Claude continue, the final turn span and answer are sent when the next human prompt arrives or the session ends. In an interactive session, finish the session or send your next prompt to see the completed turn.

Export has a bounded deadline and never changes the agent's answer or exit status. Delivery is best effort: process termination, oversized turns or an unavailable destination can lose spans. Oversized turns are skipped without blocking later turns. Ambiguous or partially accepted batches are not blindly retried. Turns observed with tracing or content capture off are excluded from later reconstructed history; re-enabling does not backfill them.

## Troubleshooting

**No trace appears.** Run `status claude --json`, then `doctor claude`. Check registration, the reported Claude Code version, capture/export errors and the last accepted export time. Restart sessions opened before setup. The integration needs Claude Code's transcript persistence; do not launch with `--no-session-persistence`.

**Setup reports `NATIVE_TRACING_CONFLICT`.** Disable `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`, `ENABLE_ENHANCED_TELEMETRY_BETA` and `ENABLE_BETA_TRACING_DETAILED` in Claude settings and the launching environment, then retry. Hooks and native generation tracing are exclusive; using both would duplicate observations. Setup does not change your existing telemetry settings.

**Settings cannot be updated.** The installer refuses symlinked, unsafe, malformed or unsupported settings files rather than overwriting them. Keep existing settings valid JSON and owned by your user. Organization policy may also prevent user hooks from running.

**Doctor reports `PROJECT_CONFIG_IGNORED`.** The repository configuration is malformed or unsafe, so it is ignored. Tracing uses your protected user configuration instead. Keep the configuration valid and move the project to an owner-controlled directory, or correct permissions before relying on project-specific routing or privacy.

**Doctor reports `HOST_HOOKS_DISABLED`.** Claude Code's `disableAllHooks` setting prevents tracing hooks from running. Review that setting and re-enable hooks to resume tracing. Setup does not change it, and an earlier successful export does not mean hooks are currently enabled.

**Doctor reports `RUNTIME_MISSING_OR_CHANGED`.** Restore your Node.js installation, then run `npx @traceai/agents update claude`. The hooks reuse your existing interpreter instead of copying it. Updates and uninstall remove the old owned hook bundle.

**The agent works while export fails.** This is intentional. Check the destination and credentials with `doctor`; tracing must not prevent you from using Claude Code.
