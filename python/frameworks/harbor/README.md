# Harbor tracing for Future AGI

Send Harbor trial results and saved ATIF trajectories to Future AGI as OpenTelemetry traces. This host-side adapter reuses Harbor's official `harbor-atif2otel` converter; it does not instrument agents inside their sandboxes.

Requires Python 3.12–3.13 on macOS or Linux, Harbor 0.25.0 and harbor-atif2otel 0.1.1. The dependencies are pinned because the result schema and converter behavior are part of the adapter's contract.

## Set up

```bash
pip install traceai-harbor
export FI_API_KEY="your-api-key"
export FI_SECRET_KEY="your-secret-key"
export FI_PROJECT_NAME="harbor-evaluations"
```

Add `--plugin futureagi` to your existing Harbor run. Keep your existing task, agent, model and environment options. For an existing `job.json` configuration:

```bash
harbor run --config ./job.json --plugin futureagi
```

Do not also enable the `atif2otel` uploader for the same destination. The adapter already calls its converter. Credentials stay in the host environment; never put them in plugin kwargs, agent environment settings or task files.

In Future AGI's Observe view, open `harbor-evaluations`. Each finalized trial has a trace containing measured lifecycle phases, verifier reward attributes and the available reconstructed AGENT, LLM and TOOL spans. Trials without usable trajectories still have a result envelope marked `harbor.trace.incomplete=true`.

## Import a finished job

```bash
traceai-harbor import-job ./jobs/my-harbor-job
```

The command prints counts of sent, skipped, failed, conflicting and incomplete trials. It exits nonzero on delivery or artifact failures. Fix the configuration or delivery issue and run it again; successfully acknowledged traces are skipped. The plugin also reconciles final results at job end.

Keep the `.traceai-harbor` directory with the job when moving it. It stores locked, atomically written acknowledgement digests, not credentials or trace content. A different endpoint, project, credential pair or content policy has separate acknowledgement state. A changed previously exported trace is reported as a conflict, never silently overwritten. Preserve the original artifacts; run a new Harbor job to record a revised execution rather than deleting acknowledgement state.

A process crash after the server accepts a request but before acknowledgement is saved can cause retransmission. Partial OTLP responses are unacknowledged and retried on the next import, so accepted spans in a partial response can also repeat. Deterministic span IDs do not guarantee server-side deduplication.

## Content and scope

Metadata-only export is the default. Prompts, responses, tool arguments/results, reasoning, schemas, arbitrary metadata and exception text are omitted. Task/trial/agent/model names, identifiers, phase timing and finite reward/usage values remain metadata; do not put sensitive data in names.

To include bounded input/output text:

```bash
harbor run --config ./job.json --plugin futureagi --plugin-kwarg futureagi.capture_content=true
```

For finished jobs, add `--capture-content`. Known FI credentials, common credential fields/token patterns and local paths/URLs are redacted. This is not a general PII detector: review your agent's content before opting in. Strings are limited to 2 KiB; reasoning and tool schemas remain excluded. Enabling content changes the export policy and may send traces previously exported metadata-only.

The default full trace endpoint is `https://api.futureagi.com/tracer/v1/traces`. `FI_HARBOR_ENDPOINT` overrides it without appending a suffix. Use HTTPS outside loopback. Redirects are rejected and environment HTTP proxies are ignored.

ATIF v1.7 is supported. Embedded subagents are converted by the upstream package; external and continuation references are not fetched and mark the trace incomplete. Native step/tool timing is approximate and content is reconstructed, not a recording of live provider calls. Missing or out-of-range native timing uses explicit trial-bound fallback attributes. Standard token/cost accounting is retained only on LLM spans; trial/root totals are non-accounting `harbor.summary.*` attributes.

Verifier rewards are trace attributes, not Future AGI dataset rows, experiments or score records. Zero reward is not an instrumentation error. Harbor's `reward_valid` and `failure_cause` are preserved. Only artifacts still present can be imported; historical retry attempts overwritten by Harbor cannot be reconstructed.

Artifacts are local-only, bounded (8 MiB per JSON file, 50,000 JSON nodes, depth 24; 4,096 expanded native conversion units and 8,192 spans per trial), and symlinks are rejected. Multi-step trials are limited to 128 saved steps. No remote artifact lookup, logs or metrics are exported. Transfers are serialized with backpressure and bounded retries; a failing endpoint can delay finalization but does not change rewards or task outputs. Use the import command's exit status for delivery automation: Harbor may isolate plugin failures.
