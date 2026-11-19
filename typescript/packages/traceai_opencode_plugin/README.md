# Future AGI for OpenCode V2

Trace each OpenCode prompt, its model requests, and its tools in Future AGI. Conversations are grouped into sessions. This package uses OpenCode's V2 `Plugin.define` API, not the V1 plugin contract.

## Quick start

1. Get your API key and secret key from [Future AGI](https://app.futureagi.com/dashboard/keys).
2. Run `npx @traceai/agents setup opencode --project coding-agents`. Enter your keys at the masked prompts. Setup validates the destination, saves a private profile, and installs this plugin.
3. Restart OpenCode, use it normally, and open the project in Future AGI **Observe**. If you use OpenCode's shared service, restart it with `opencode service restart`.

Requires OpenCode V2 **2.2.5** and Node.js **22.16+** or **24**. With an existing Future AGI profile, the native install is `opencode plugin add @traceai/opencode-plugin@0.1.0`.

## Privacy and control

Content capture is on by default. Prompts, model messages, tool arguments, and tool output can contain file contents and secrets. Each field is bounded to 20,000 characters and redacted on a best-effort basis; this is not a data-loss-prevention guarantee.

Use `setup --no-capture-content` or `FI_AGENT_CAPTURE_CONTENT=false` for structure-only tracing. Project settings in `.opencode/futureagi.json` can reduce capture but cannot raise a user-approved privacy ceiling. Existing stricter `FI_HIDE_*` settings remain in force.

- Run `npx @traceai/agents status opencode --check` from your project directory to include project privacy and native plugin disables. It shows registration, observed activation, endpoint validation, and accepted exports separately. `doctor opencode` uses the same project-aware defaults; `--project-dir /path/to/project` inspects another project.
- `npx @traceai/agents disable opencode` also fences queued exports from an already running plugin.
- `FI_AGENT_ENABLED=false` disables the plugin when starting OpenCode.

Configuration and credentials are provided by `@traceai/agent-core`; credentials are never written into OpenCode's plugin registration or your repository. Environment credential overrides must contain both `FI_API_KEY` and `FI_SECRET_KEY`.

Saved keys are bound to the approved endpoint and project. To switch, run `npx @traceai/agents setup opencode --project my-project`, approve the displayed destination, and restart OpenCode. Changing only `FI_PROJECT_NAME`, `FI_BASE_URL`, or `FI_OTLP_ENDPOINT` to another destination blocks tracing with `DESTINATION_NOT_APPROVED`; a process-only override requires an explicit complete environment key pair as well. Repository-selected transport, project, or profile changes are not approved merely because keys are in the environment.

If saved keys are revoked, rerun setup in a terminal without `--json`. After authentication rejection it prompts for replacement keys without echoing them and saves them only after successful validation. Unset both credential environment variables first if you want to replace saved keys rather than use environment credentials.

## Trace behavior

A delivered user prompt creates one immutable AGENT root. Actual model attempts create LLM children; tool calls, child sessions, retries, and automatic compaction remain under that prompt. Background children and their completion-triggered continuations stay in the original trace even if you send another prompt before they finish.

Model generations own their token counts. Automatic compaction contributes its separate summary usage exactly once, including summary-template retries, as an aggregate activity rather than an invented model request. Known catalog costs are marked **estimated**, not provider billing; unknown costs are omitted.

The observer uses public events and neutral hooks. It does not consume or replace HTTP/WebSocket streams, change model configuration, or register a global OpenTelemetry provider. Exports are bounded, memory-only, and fail open. A killed process can lose pending data. Auxiliary title requests and requests outside a conversation are not included; manual compaction without an active prompt does not invent a new prompt trace.

Remove the old copied `futureagi-otlp.ts` recipe before installing this package; the legacy recipe and this plugin must not export the same work simultaneously.
