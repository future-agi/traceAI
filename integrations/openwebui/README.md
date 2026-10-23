# Open WebUI → Future AGI (filter function)

`futureagi_filter.py` is an Open WebUI
[Function](https://docs.openwebui.com/features/extensibility/plugin/functions/filter)
of type filter. It turns each completed chat turn into a Future AGI trace. It
does not modify Open WebUI and does not import traceAI.

Tested end to end with Open WebUI 0.11.4 (`pip install open-webui==0.11.4`),
an OpenAI-compatible model connection and a local Future AGI stack. The UI
chat was streamed through a real browser, and direct API calls were checked
on both streaming and non-streaming paths.

## Install

1. In Open WebUI, open **Admin Panel → Functions** and create a function.
   Paste the whole of `futureagi_filter.py` and save. Keep the opening `"""`
   on its own first line: Open WebUI reads the title and requirements from
   that block only then.
2. Open WebUI installs the frontmatter requirements when
   `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=true` (the default). It does
   not install them when `OFFLINE_MODE=true`; saving then fails visibly if
   the OTel exporter is unavailable. The filter requires the OTLP/HTTP
   exporter and `certifi`. `opentelemetry-api` and `opentelemetry-sdk` are
   intentionally not listed: Open WebUI supplies the SDK. Keep the exporter
   and SDK on the same OpenTelemetry release line; if your image pins an SDK,
   use its matching exporter release and restart after changing packages.
3. Turn the function on, and either mark it **Global** (every model) or add it
   to the models you want traced (**Workspace → Models → model → Filters**).
   A filter that is off, or neither global nor attached to a model, never runs.
4. Open the function's **Valves** (gear icon) and set:

| Valve | Default | Meaning |
|---|---|---|
| `api_key`, `secret_key` | empty | Future AGI keys, sent as `X-Api-Key` and `X-Secret-Key`. They are password fields. With either one empty, nothing is exported. |
| `endpoint` | `https://api.futureagi.com` | Future AGI origin. The filter posts OTLP/HTTP to `{endpoint}/tracer/v1/traces`; a value already ending in that path is used as is. |
| `project` | `open-webui` | Future AGI project, sent as `project_name` and `project_type=observe`. |
| `redact` | `true` | Leaves prompts and replies out of every span. **Set `redact=false` to see prompts and replies in Observe.** |
| `include_email_hash` | `false` | Adds a pseudonymous HMAC of the normalised email. `redact=true` prevents content capture; with `redact=false`, the email can appear inside captured prompt/history content even though it is not emitted as a standalone user attribute. |
| `email_hash_salt` | empty | Optional admin-controlled HMAC salt. Use a secret value if email joins must be resistant to offline candidate testing. |
| `max_chars` | `32000` | Maximum characters copied into each input/output value. `0` means unlimited. |
| `priority` | `0` | Open WebUI filter priority; lower values run first. Put a PII-scrubbing filter before this one when `redact=false`. |

Enable `ENABLE_VALVE_ENCRYPTION=true` so Open WebUI encrypts these valves in
its database. Do not rely on the default plaintext storage for production
keys.

## What a turn looks like

Open WebUI calls the filter's `inlet` before the model call, its `stream`
hook for decoded streaming events, and its `outlet` after the reply. The
filter stores turn state on Open WebUI's request `__metadata__`; it keeps no
per-turn message dictionary. Each completed turn becomes one trace of two
spans:

| Span | Type | Attributes |
|---|---|---|
| `openwebui.chat` | chain (`fi.span.kind=CHAIN`) | `session.id` and `gen_ai.conversation.id` (the Open WebUI chat id), `user.id`, optional pseudonymous `user.email_hash`, and with `redact=false` `input.value` and `output.value` |
| `openwebui.llm`, its child | llm (`fi.span.kind=LLM`) | model, usage, the same ids, and with `redact=false` the conversation JSON as `input.value` plus the reply as `output.value` |

The root is explicitly detached from Open WebUI's own current server span, so
it is a real Future AGI trace root even when native Open WebUI tracing is on.
The LLM span is a child of that root. The LLM span's latency is **turn
latency**, not model-only latency: it includes Open WebUI preprocessing,
retrieval, tools, database work and response publishing between `inlet` and
`outlet`.

- The exporter is cached per valve configuration and uses a bounded batch
  queue with explicit delay, batch-size, and timeout values. Open WebUI's
  process-wide `OTEL_BSP_*` environment variables cannot change or invalidate
  this filter's processor. The chat does not wait for Future AGI, and a
  collector outage does not break the chat. A process killed before its queue
  flushes can lose spans.
- For ordinary replies, usage reads `prompt_tokens`/`completion_tokens`.
  For a reply containing function-call output items, it reads cumulative
  `input_tokens`/`output_tokens` from Open WebUI's native tool loop. Total is
  computed as input plus output because Open WebUI can double-count its stored
  `total_tokens` when providers repeat usage chunks.
- A provider error observed by the filter's `stream` hook is copied to the
  request `__metadata__` and survives until `outlet`. On the stock SSE path,
  Open WebUI parses a `data:` line and invokes `process_filter_functions` with
  the decoded event before it emits that event, so the filter can mark a
  partial-content outlet `ERROR` without discarding the partial content. The
  filter also accepts a raw JSON/bytes line for deployments that explicitly
  adapt the host stream branch, but that is not stock Open WebUI behavior.
- **Stock Open WebUI 0.11.4 limitation:** in `open_webui/utils/middleware.py`
  lines 5071-5093, a plain JSON provider error line such as
  `{"error": ...}` is parsed, persisted/emitted, and then `continue`s before
  `process_filter_functions(..., filter_type='stream', ...)` at lines
  5101-5109. The Function therefore receives no `stream` call and no failure
  marker is added to request `__metadata__`. Later, `outlet_filter_handler`
  builds the outlet body from selected message fields at lines 4123-4143;
  the error is not included there either. A filter-only implementation cannot
  distinguish a successful no-usage reply from a prior plain-JSON error after
  this branch, so this path cannot be marked reliably as `ERROR` without an
  Open WebUI change. Empty replies with no usage are still marked `ERROR`;
  failures that bypass both the stream hook and outlet can produce no
  application span.
- Upstream change needed: invoke stream filters for the plain JSON error
  object before the branch's `continue`, or expose that error in the metadata
  or outlet payload passed to Function filters.
- An outlet call without a matching inlet is skipped. This avoids duplicate
  zero-duration traces from the legacy `/api/chat/completed` endpoint.

## Live model verification (2026-10-19)

Re-run against the real OpenAI API (`https://api.openai.com/v1`, `gpt-4o-mini`) as the Open WebUI model connection, instead of a local stand-in. Open WebUI 0.11.4 (`pip install open-webui==0.11.4`, Python 3.12), filter `futureagi_filter.py` version 0.3.0 installed through the Open WebUI API as in the install steps, exporting to a project on a local Future AGI. 0.12.3 was not re-run.

| Turn | Trace id in Observe | Result |
| --- | --- | --- |
| Plain, default valves (`redact=true`) | `0ed539cf-1c03-5a63-e9c0-24a2b04964bc` | `openwebui.chat` (chain) with child `openwebui.llm`, model `gpt-4o-mini`, 3189 in / 2 out tokens, session = chat id, no content |
| Native tool call (`add`) | `43ac29ee-713e-368e-a601-e6cd784c29cc` | same two spans, 6471 in / 31 out tokens (cumulative over the tool loop), no content |
| Plain, `redact=false` | `6d2b79b4-ab6e-214a-8abe-4fb776d1ef11` | prompt and reply stored (`input` 53 B / `output` 2 B on the chain span) |
| Unknown model id | none | see below |

Privacy: with `redact=true` the unique marker (in the prompt of both turns) was in none of the 4 stored rows, in any column, and the OpenAI key was not either. With `redact=false` the marker was in the 2 rows of that turn.

Unknown model id: a model id that Open WebUI does not list is rejected by Open WebUI itself with HTTP 400 `Model not found` before any filter runs, so there is no trace. With the id listed in the connection (`gpt-4o-mini-not-a-model`), OpenAI returned HTTP 404 `model_not_found`; Open WebUI logged the failure and the filter exported **no span**. This is the stock-0.11.4 limitation already described above (a plain provider error is not passed to the filter), now confirmed against the real API: an upstream error does not produce an ERROR span.

## Privacy

- Content is off by default (`redact=true`): no message text leaves Open
  WebUI. Ids, model and token counts remain visible in Observe.
- Set `redact=false` to see prompts and replies in Observe. For a saved chat,
  the stored history is copied; it does not necessarily include the system
  prompt, RAG context or tool results. For a temporary chat, Open WebUI's
  processed request may include system/RAG context, so `temporary:` and
  legacy `local:` chat ids force redaction even when `redact=false`.
- `include_email_hash` is pseudonymous, not anonymous: the filter hashes
  `email.strip().lower()` with HMAC-SHA256. An optional salt valve is
  available. The raw email is not put in attributes by this filter, although
  an admin who explicitly turns content capture on should treat prompt text
  as capable of containing any user field.
- The Future AGI keys travel only as request headers, never as span
  attributes. `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME`, sampler,
  `OTEL_BSP_*`, certificate, client-key, timeout, and compression environment
  variables are not inherited by this filter's provider: it supplies an
  explicit resource, `ALWAYS_ON` sampler, safe batch settings, a 30-second
  request timeout, no request compression, and the system CA bundle.
  `OTEL_SDK_DISABLED=true` still disables the OpenTelemetry SDK globally,
  including this filter; unset it when the filter should run.

## Native Open WebUI tracing

The native Open WebUI exporter (`ENABLE_OTEL` and
`ENABLE_OTEL_TRACES`) is a separate path for HTTP, database and Redis spans.
The filter's root is detached from that server span, so its application traces
are not parented under native infrastructure traces. They remain separate
traces in Future AGI; enable native tracing only if you also want the
infrastructure view.

## Limits

- Model ids are Open WebUI's, which may differ from the provider's model name.
- A failed non-streaming request or user Stop can bypass `outlet`, so no
  application span is possible through this Function hook.
- Each Open WebUI worker process exports on its own. Function edits create a
  new instance; the old provider is finalised and shut down. Spans still
  queued when a worker is killed are lost.

## Tests

From the repository root:

```bash
uv run --no-project --with pytest --with pydantic \
  --with opentelemetry-sdk --with opentelemetry-exporter-otlp-proto-http \
  pytest integrations/openwebui/tests/test_filter.py --noconftest -q
```

The tests call the hooks with the arguments Open WebUI 0.11.4 passes
(`body`, `__user__`, `__metadata__`; valves set on the instance before each
hook), check the real Open WebUI frontmatter parser when that package is
available (otherwise the pinned parser rules are reproduced), and export to
an in-memory exporter or a loopback OTLP/HTTP server. They cover detached
roots, tool-loop usage, in-stream empty-reply errors, env isolation, bounded
content, temporary-chat redaction, valve caching, and the no-inlet duplicate
case. No external network is used by the unit tests.
