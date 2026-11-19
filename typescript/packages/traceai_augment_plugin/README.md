# Future AGI Auggie hooks

Package-owned Auggie hooks collect response summaries and tool activity, grouped by conversation in Future AGI. This private package is not a published install route.

## Setup contract

Once the package is released, the unified CLI provides the following three-step workflow:

1. Create an API key and secret key on the [Future AGI keys page](https://app.futureagi.com/dashboard/keys).
2. Run `npx @traceai/agents setup augment --project coding-agents`. Setup collects masked credentials or reads `FI_API_KEY` and `FI_SECRET_KEY`, validates the destination, and installs the maintained hooks. Captured content can include file text and secrets the agent reads.
3. Restart Auggie, use it normally, then inspect the project in Future AGI. Run `npx @traceai/agents status augment --check` to distinguish installed hooks, observed activation and accepted exports.

No repository clone, copied emitter, background daemon or customer-maintained script is needed. The installer merges native settings and keeps its bundled runtime outside the project. The Node executable is pinned at setup, so hook execution does not depend on the interactive shell's PATH or an npm cache.

## VS Code and IntelliJ

The `augment-ide` route is unavailable for setup. It reports `AUGMENT_IDE_GUI_QUALIFICATION_REQUIRED` before asking for credentials or changing hooks. `status augment-ide --json` and `disable augment-ide` remain available. Do not substitute `setup augment` as an editor installation command.

The editor adapter reuses the same mapper, encrypted state and packaged runtime, with separate registration ownership and disable state. Discovery reads only installed extension manifests: `augment.vscode-augment` in VS Code and `com.augmentcode` in IntelliJ. Presence is not enablement or evidence that hooks ran. Shared Augment user hooks do not identify which host invoked them.

The editor registration profile targets only `~/.augment/settings.json`, which Augment documents for both editors. It does not reuse the CLI's project hook locations or require `auggie` on the GUI's PATH. A `.augment/futureagi.json` project privacy override is independent of native hook registration. Editor setup, per-editor version support and real trace screenshots remain unavailable until both host profiles have their own qualification; no CLI or fixture result establishes editor support.

References: [Augment hooks](https://docs.augmentcode.com/cli/hooks), [VS Code extension](https://marketplace.visualstudio.com/items?itemName=augment.vscode-augment), [JetBrains plugin](https://plugins.jetbrains.com/plugin/24072-augment).

## Configuration

| Setting | Effect |
| --- | --- |
| `FI_AGENT_CAPTURE_CONTENT=false` | Stop exporting prompts, responses, tool input/output and file contents. |
| `setup --no-capture-content` | Persist structure-only mode and omit conversation metadata from the hook registration. |
| `FI_AGENT_ENABLED=false` | Immediately fence collection and export. |
| `disable augment` | Persist the kill switch without credentials or a network request. |
| `FI_USER_ID` | Associate activity with an explicit application user ID. No Augment account email is harvested. |
| `FI_OTLP_ENDPOINT` | Exact OTLP traces URL; takes precedence over `FI_BASE_URL`. |
| `setup --base-url https://your-api.example.com` | Set a self-hosted API origin through the same validated setup. |
| `setup --scope project --project-dir /path/to/project` | Register in `.augment/settings.local.json`; credentials and runtime remain outside the repository. |
| `setup --agent-dir /path/to/cache` | Use the same directory as Auggie's `--augment-cache-dir`. Pass this directory to subsequent lifecycle commands too. |

The shared core owns the protected credential store and precedence: user settings, explicit project settings, environment and invocation flags. A repository cannot raise the approved content ceiling or silently redirect saved credentials. Re-run setup to approve a destination change. Project overrides use `.augment/futureagi.json` and contain no credentials.

## What the hooks expose

- A finalized `stop-summary` root at each Stop, with the available user prompt and text response. Conversation identity groups these roots into a session.
- `PostToolUse` children with actual tool input, output, errors and reported file changes when content capture permits them.
- Explicit interrupted or incomplete outcomes. Pending tools at SessionEnd produce an incomplete summary.

Auggie 0.37.0's documented hooks do not supply stable user-turn IDs, tool-call IDs, tool durations, individual model requests, token counts or cost. The adapter does not invent these fields, reconstruct LLM spans from a final response, or claim subagent/compaction coverage. Tool timing is observation timing. Multiple Stop callbacks caused by another hook's block decision can produce multiple response summaries for one prompt.

Only Stop requests `includeConversationData`. `includeUserContext` and `includeMCPMetadata` stay off; workspace paths, raw metadata, user email and undocumented `_exchange` data are not exported. The adapter never registers a `PreToolUse` permission hook or writes output that could steer Auggie.

## Privacy and delivery

Content defaults to on. It is allowlisted, bounded to 20,000 characters per field and best-effort redacted for known credentials before encrypted persistence. This is not a general secret detector. Disable content capture before using the agent on sensitive files when appropriate. Existing stricter `FI_HIDE_*` settings are honored, including error details.

Cross-hook state uses AES-256-GCM in owner-only files, with a per-profile/destination key, bounded to 128 sessions under 64 KiB each and a 24-hour expiry. A turn retains at most 32 tools and 40 KiB of serialized state; overflow is reported as dropped data and an incomplete summary. Privacy reductions scrub buffered content. Disable fences stale processes and old generations.

Nonterminal hooks only update local state. Stop export is synchronous because Auggie's hooks are synchronous, bounded by a six-second process deadline and a 7,000 **millisecond** host timeout. Outages cannot change Auggie's result or hook decision. Export is best effort without retry after ambiguous acceptance; no exactly-once or crash-recovery guarantee is made. A missing Stop, abrupt process termination, state expiry or buffer overflow can lose observations.

## Troubleshooting

- **No registration:** make sure `auggie --version` resolves on PATH during setup. Detection is pinned to 0.37.0; do not assume another version has the same hooks.
- **Hooks installed, no exports:** restart Auggie, inspect `status augment --check`, and check whether project or enterprise settings override user hooks. Registration is not proof of activation or product readback.
- **Registration conflict:** remove the competing Future AGI registration explicitly. Setup does not overwrite edited hooks, grant trust, or remove another installation silently.
- **Custom Auggie directory:** use the matching `--agent-dir` during setup and status. Default installation is under `~/.augment`.
- **Windows:** launchers are packaged, but installation is disabled until owner-only ACLs and the native Windows script contract are qualified.

`update augment` preserves the shared configuration. `uninstall augment` removes only matching owned hooks and leaves unrelated settings and credentials intact.
