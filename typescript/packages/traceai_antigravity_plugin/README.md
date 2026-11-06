# Future AGI for Antigravity CLI

Trace each Antigravity user turn in Future AGI, with model responses and tools grouped into the same conversation session.

## Set up

Requires Antigravity CLI 1.x on Linux and Node.js 22.16+ (22.x) or 24.x. CLI updates within the supported major line use validated hook shapes; unknown shapes appear in tracing diagnostics. Antigravity IDE and App tracing is not available through this CLI integration; CLI setup does not configure the GUI.

1. Get your API key and secret key from [Future AGI keys](https://app.futureagi.com/dashboard/keys).
2. Run `npx @traceai/agents setup antigravity --project coding-agents`. Enter the keys in the masked terminal prompts. Setup validates them, stores credentials in a private file, and installs the `futureagi-tracing` plugin through `agy plugin install`. It does not change Antigravity's model configuration or tool permissions.
3. Start a new `agy` session, ask it to read a file, then open **Observe** in Future AGI and select **coding-agents**. Each user message has its own trace; **Sessions** groups messages from the same conversation.

Use `agy -p "Read README.md and summarize it"` for a single headless turn. Resume with `agy --conversation <conversation-id> -p "What should I change next?"`.

Content capture is on by default. Prompts, responses and tool content can contain file text and secrets the agent reads. To capture structure and usage without content, use `setup --no-capture-content` or set `FI_AGENT_CAPTURE_CONTENT=false` before launching Antigravity. Redaction is best effort, not a guarantee that arbitrary secrets are removed.

## Configuration

The shared credential store is `${XDG_CONFIG_HOME:-$HOME/.config}/futureagi/agents/credentials.json`, protected with mode `0600`. Antigravity-specific non-secret configuration is in `~/.gemini/config/futureagi.json`. Environment variables override saved configuration; provide both `FI_API_KEY` and `FI_SECRET_KEY` when overriding credentials.

| Setting | Purpose |
| --- | --- |
| `FI_PROJECT_NAME` | Future AGI project name. Saved credentials need explicit setup approval before changing their destination/project. |
| `FI_USER_ID` | Optional application user identifier. Otherwise setup creates an opaque identifier. |
| `FI_AGENT_ENABLED=false` | Unconditional collection and export kill switch. |
| `FI_AGENT_CAPTURE_CONTENT=false` | Omit prompt, response, tool and error content. |
| `FI_AGENT_CONFIG_DIR` | Override the shared Future AGI configuration directory. |
| `FI_BASE_URL` | Self-hosted API origin. Setup accepts `--base-url` to approve it. |
| `FI_OTLP_ENDPOINT` | Exact OTLP traces URL; takes precedence over the base URL. |

For a project-specific privacy override, add this non-secret `.futureagi/agents.json` in the workspace:

```json
{"antigravity":{"schemaVersion":1,"captureContent":false}}
```

The native plugin is user-global. Project configuration is resolved only when the hook identifies one workspace. Multi-root sessions use the user configuration. A repository cannot increase the user's content-capture permission or redirect saved credentials to another destination. Do not put keys in project files.

## Status and troubleshooting

- `npx @traceai/agents status antigravity --check` checks registration, credential acceptance, configuration provenance and recent exports. HTTP acceptance and product readback are different states.
- `agy plugin list --json` shows the native plugin. If it is disabled in Antigravity, enable it there; setup does not silently override a host-disabled plugin.
- Run setup from the workspace you use with Antigravity. Remove a previous `fi-hook-to-otlp` registration before setup; the installer checks user hooks and that workspace's `.agents/hooks.json` before installing another exporter. Use `--project-dir` to check a different workspace.
- `PROJECT_CONFIG_IGNORED` means a workspace configuration could not be safely read (for example, on a group-writable external volume). Tracing continues with your user settings. Set `FI_AGENT_CAPTURE_CONTENT=false` before launching Antigravity if you need content disabled there, or correct the directory permissions.
- `PINNED_NODE_MISSING` means the Node interpreter used by the installed hooks is gone. Install Node and run setup again to repair the command.
- `PENDING_TURNS` and the `pendingTurns` field in JSON status mean source turns have not yet finalized. A live CLI session can legitimately have a pending turn. On CLI exit, the plugin recovers a turn that received no terminal hook as **incomplete**, preserving completed model responses and their usage. Uncorrelated child work stays pending instead of becoming an unrelated user trace.
- A missing transcript still permits invocation/tool structure, but model generations and token usage need the conversation's transcript. Start a new turn after installing the plugin.
- Network errors do not change the agent's result. Final export has a bounded deadline; unsuccessful or ambiguous batches are not replayed. Status reports dropped exports.

Use `npx @traceai/agents disable antigravity` to stop collection and sending. `uninstall antigravity` also removes the owned native plugin while preserving unrelated plugins and modified files.

## Trace details and privacy

Invocation activities are not provider-request spans: Antigravity's invocation hooks can enclose tool execution. Model generations come only from version-qualified `PLANNER_RESPONSE` transcript records correlated to those activities. Generation inputs are reconstructed current-turn context, not complete API/system-prompt capture. Per-response token counts belong to generations once; session/turn totals are not added a second time. Reported source output tokens already include thinking tokens; unavailable breakdowns and costs remain absent.

Tool input, available output and errors are captured. Tool/model timestamps use source records or hook observation; a completion-only hook does not establish an execution duration. A Stop hook does not finalize a turn while `fullyIdle` is false. Cancellation and incomplete work are not marked successful. If the CLI exits without Stop, the outcome is **incomplete**, because process exit alone does not establish why the turn ended. Native subagents stay under the invoking tool in the originating prompt trace, with the same session identity. Ownership comes from the native `invoke_subagent` result, not from tool names or prompt similarity. Delayed subagent completion can reset the invocation counter; it remains in the original trace. Only a new explicit user-input transcript record splits an unfinished turn. Compaction is not inferred.

Hooks use a bundled runtime installed with the plugin, not copied project scripts or a model gateway. They return neutral JSON, never approve permissions and never inject instructions. A session-scoped exit observer starts automatically for a user conversation. It watches the CLI process identity (including its start time to prevent PID reuse), then finalizes any pending turn and exits. If a hook crashed while saving state, recovery waits for its lock to expire and retries for up to 45 seconds rather than abandoning the final turn. It is not a persistent service and stops after at most 24 hours or when tracing is disabled.

Sanitized pending-turn state is encrypted and stored in owner-only files with bounded size and a 24-hour expiry. Known Future AGI keys and recognizable credential fields—including secret-access-key and private-key aliases—are redacted before persistence and before the 20,000-character text cap. Hooks use your existing Node interpreter rather than copying it, and load the exporter only when finalizing a turn. Overlapping events wait for a crash-recoverable lock; checkpoint replacement does not acquire a second lock. Terminal capture waits up to six seconds for completed transcript responses, including interrupted and failed turns, and up to six seconds for child checkpoints. Hook deadlines are ten seconds for ordinary events and twenty-six seconds for Stop, including lock waiting and export. Existing stricter `FI_HIDE_*` settings remain effective. IDE/App events, arbitrary transcript paths and artifacts are not read.
