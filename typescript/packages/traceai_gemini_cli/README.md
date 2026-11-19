# Future AGI for Gemini CLI

This package supplies the Gemini CLI adapter for the unified Future AGI agent CLI. It installs maintained, daemon-free hooks; it does not require a collector, copied instrumentation scripts, or a model proxy.

## Configuration

The shared agent CLI owns credential validation, setup, status, enable, disable, update and uninstall. Credentials remain in its protected central profile. Gemini's user-level `settings.json` contains only absolute paths to the maintained runtime, not API keys. Existing settings, comments and unrelated hooks are preserved.

The adapter uses Gemini's `GEMINI_CLI_HOME` override when set. Project overrides live in `.gemini/futureagi.json`; project files cannot redirect saved credentials or raise the user's content-capture ceiling. Hook execution uses the working directory provided by the host process, not paths supplied inside the event payload.

The hook adapter is an **exclusive alternative** to native Gemini telemetry. Turn off an existing Gemini native-telemetry configuration before registering this adapter. The installer does not silently reconfigure another exporter or approve workspace trust. Open Gemini in your project and complete its normal trust prompt when requested.

## What is captured

- A turn root for each `BeforeAgent` / `AfterAgent` boundary, grouped by conversation in Sessions.
- Model response chunks collected under their observed model request. A `BeforeModel` event alone does not become an LLM span: the request may have been blocked or replaced by another hook.
- Text input and output exposed by the model hooks, with reported input, output, cache and reasoning usage. Usage snapshots replace earlier snapshots; streamed chunks do not multiply token totals.
- Tool results and errors from `AfterTool`, and compaction markers from `PreCompress`.
- An explicitly configured or opaque local user identity, rather than the OS account name.

Gemini's stable model-hook API excludes non-text parts. Tool hooks do not expose a source call ID or execution start time, so tool spans mark the completion event, not a measured execution duration. Cost and subagent ancestry are not inferred. Session-end delivery is best effort; a missing terminal event is not a successful turn.

## Privacy and delivery

Content capture follows the shared profile and is on by default. Captured text can include file contents and secrets the agent reads. Set `FI_AGENT_CAPTURE_CONTENT=false`, or use the shared setup command's `--no-capture-content` option, to retain structure without content. Existing stricter input, output and error-detail privacy settings remain effective. Known Future AGI credentials receive best-effort redaction; this is not general-purpose secret detection.

Recovery state is allowlisted, encrypted with AES-256-GCM and stored with mode `0600`. It is bound to the original destination, privacy settings and disable generation. Changing these discards incompatible buffered state. Text fields are capped at 20,000 characters; a bounded turn buffer may omit additional content and report an incomplete trace. The adapter expires pending state after 24 hours and limits local state to 64 MiB.

Hooks return neutral JSON and never modify prompts, responses, tool permissions or model configuration. Nonterminal hooks perform local work only. Terminal export has a bounded deadline and cannot change the agent's exit status. The persistent kill switch fences collection and sending. Accepted or ambiguous batches are not blindly retried; delivery is not exactly-once.

## Troubleshooting

- **Native telemetry conflict:** disable the other Gemini telemetry route before using this hook adapter. Running both routes creates duplicate or unfiltered observations.
- **Workspace trust required:** open the project in Gemini and approve it through Gemini's own trust flow. Setup does not grant trust.
- **Unregistered or changed hooks:** inspect the shared CLI's status output. Update does not overwrite a hook command that someone has modified.
- **No last export:** distinguish installation, observed activation and an accepted OTLP export in status. A parsed configuration alone does not establish a connection or product readback.
- **Unsupported version or platform:** use the host version listed in the adapter registry. Windows owner-only ACL support is not supplied by this package.
