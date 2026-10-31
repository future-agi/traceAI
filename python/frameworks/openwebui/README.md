# traceAI-openwebui

Trace each model call made through an Open WebUI Pipe Function in Future AGI, with streaming duration, provider-reported usage, and failure or cancellation status.

## Install

Use Python 3.11 or 3.12 and Open WebUI 0.11.4–0.12.x. Configure an OpenAI-compatible connection model in Open WebUI first. Native Ollama connections are not supported: Open WebUI's conversion can hide failures and incomplete streams, so the Pipe rejects that route rather than recording misleading spans.

Install the package in the same Python environment as Open WebUI, then generate its Function import file:

```bash
pip install traceAI-openwebui
traceai-openwebui export-function --output traceai-openwebui.json
```

For a container deployment, install the package in your image and run the export command in that image. Make the generated JSON file available to your browser. The file contains a small package loader, not instrumentation source to maintain.

## Configure the Function

1. In **Admin Panel → Functions**, import `traceai-openwebui.json`.
2. Open the **Traced model** Function's valves. Set `target_model` to the exact model ID from your existing OpenAI-compatible connection. Native Ollama models, workspace model presets, other Pipes, and Arena models cannot be targets.
3. Set `api_key`, `secret_key`, and `project` to your Future AGI credentials and project name. The default endpoint is `https://api.futureagi.com`. For self-hosted Future AGI, set its HTTPS origin; `/tracer/v1/traces` is added automatically.
4. Enable the Function, and select **Traced model** in a new chat. Access to the target model is still checked using the signed-in Open WebUI user.

Set `ENABLE_VALVE_ENCRYPTION=true` in Open WebUI before storing production credentials so its database encrypts the valves.

Use the wrapper model's settings for tools and knowledge. Target-model workspace presets are not inherited. Enable **Usage** in the wrapper model's capabilities when your provider supports streamed token counts. Missing usage stays missing; the integration does not estimate it.

Send a message such as “Explain how a cache works in two sentences.” In Future AGI, open **Observe**, choose your project, and inspect an `openwebui.model` span. Native tool loops that invoke this Pipe again produce a separate span for each actual model call, not one span for the whole conversation.

## What is captured

- One `LLM` span per delegated model request, from dispatch through response consumption.
- Provider-reported input and output token counts on that call only. Repeated usage snapshots replace previous values; turn totals are never added again.
- Fixed error messages for failures, cancellation, or an incomplete stream. Provider exception text and stack traces are not exported.
- The active OpenTelemetry span as parent, when one exists. The package does not export Open WebUI's native HTTP, database, Redis, or authentication spans.

This is **model-call tracing**, not a trace of the entire chat workflow. Retrieval, tool execution, approval pauses, and calls to unwrapped models are outside the Pipe boundary. The integration does not fabricate spans for them or reconstruct model calls from the final transcript. If you also instrument the target gateway, avoid counting both its LLM spans and this Pipe's spans as separate model usage.

Prompts, responses, user identifiers, raw email, headers, URLs, tool arguments, and retrieval content are excluded by default. To include message content, enable `capture_content`; `max_chars` bounds each captured input/output to 32,000 characters by default. Temporary and local chats always exclude content. Model IDs and your configured project name are exported.

Open WebUI's own telemetry is independent. Enabling it can export additional personal data through its separately configured exporter; this package does not sanitize that exporter.

## Replace the old Filter

Disable and delete the old Future AGI Filter Function before enabling this Pipe. The old filter recorded whole-turn summaries as an LLM child and could not observe failed or cancelled turns reliably. The Pipe is not a drop-in filter: choose **Traced model** for the calls you want to trace. Remove any separate tracing filter or gateway integration that would duplicate usage.

Existing Function copies in Open WebUI's database do not change when this repository changes. Import the generated Function after each package upgrade, then restart Open WebUI to load the new Python package.

## Troubleshooting

**No spans:** Confirm you selected **Traced model**, supplied both Future AGI keys, and enabled the Function. Tracing configuration/export failures do not block the model response. Allow a few seconds for the batch exporter.

**Target model rejected:** Use an OpenAI-compatible connection model ID, not a native Ollama model, workspace preset, Arena model, or another Pipe. The signed-in user must also have permission to use that target.

**No token counts:** Enable Usage on the wrapper model and check that the provider returns usage. Cancelled calls may never receive a final usage snapshot.

**Incomplete stream:** The provider ended its stream without a completion marker, or the consumer closed it early. The span is not marked successful. An abrupt process kill cannot export pending spans.

**Self-hosted collector:** Remote endpoints must use HTTPS; HTTP is accepted only for loopback testing. Redirects, host proxy settings, `.netrc`, and unrelated native OTLP client certificates are not used. Use a directly reachable collector endpoint.

## Embedding lifecycle

If you manage the Pipe lifecycle in Python, `pipe.flush(timeout_millis=10000)` bounds the caller's wait and returns `False` when the flush does not complete within that budget. An in-progress export can continue in the background; retrying does not create concurrent flush workers. Call `pipe.close()` to retire the exporter after active model calls finish.
