# Open WebUI → Future AGI (filter function)

`futureagi_filter.py` is an Open WebUI
[Function](https://docs.openwebui.com/features/extensibility/plugin/functions/filter) of type
filter. It turns each chat turn into a Future AGI trace. It does not modify
Open WebUI and does not import traceAI: Open WebUI installs the three
OpenTelemetry packages named in the file's frontmatter into its own
environment when the function is saved. Open WebUI 0.11.4 already ships the
OpenTelemetry SDK; the OTLP/HTTP exporter is the one it adds.

Tested end to end with Open WebUI 0.11.4 (`pip install open-webui==0.11.4`),
an OpenAI-compatible model connection and a local Future AGI stack: chats
typed in the Open WebUI page, streamed, arrived as traces in the project set
in the valves.

## Install

1. In Open WebUI, open **Admin Panel → Functions** and create a function.
   Paste the whole of `futureagi_filter.py` and save. Keep the opening `"""`
   on its own first line: Open WebUI reads the title and the requirements from
   that block only then.
2. Turn the function on, and either mark it **Global** (every model) or add
   it to the models you want traced (**Workspace → Models → model → Filters**).
   A filter that is off, or neither global nor attached to a model, never runs.
3. Open the function's **Valves** (gear icon) and set:

| Valve | Default | Meaning |
|---|---|---|
| `api_key`, `secret_key` | empty | Future AGI keys, sent as the `X-Api-Key` and `X-Secret-Key` headers. Shown as password fields. With either one empty, nothing is exported. |
| `endpoint` | `https://api.futureagi.com` | Future AGI origin. The filter posts OTLP/HTTP to `{endpoint}/tracer/v1/traces`; a value that already ends in that path is used as is. For a self-hosted collector use its origin. |
| `project` | `open-webui` | Future AGI project; sent as the resource attributes `project_name` and `project_type=observe`. |
| `redact` | `false` | `true` leaves prompts and replies out of every span. |
| `include_email_hash` | `false` | `true` adds `user.email_hash`, a SHA-256 of the user's email. The email itself is never sent. |

Open WebUI stores the valves in its database (encrypted when
`ENABLE_VALVE_ENCRYPTION` is on) and shows them to admins.

## What a turn looks like

Open WebUI calls the filter's `inlet` before the model call and its `outlet`
after the reply. Each turn becomes one trace of two spans:

| Span | Type | Attributes |
|---|---|---|
| `openwebui.chat` | chain (`fi.span.kind=CHAIN`) | `session.id` and `gen_ai.conversation.id` (the Open WebUI chat id, so a chat is one Future AGI session), `user.id` (the Open WebUI user id), `user.email_hash` (opt-in), `input.value` (the user's message) and `output.value` (the reply) |
| `openwebui.llm`, its child | llm (`fi.span.kind=LLM`) | the same ids, `gen_ai.request.model` / `llm.model_name` (the Open WebUI model id), `gen_ai.usage.input_tokens` / `output_tokens` and `llm.token_count.*` when the reply carries usage, otherwise `gen_ai.usage.unavailable=true`, `input.value` (the conversation sent, as JSON) and `output.value` (the reply) |

- Both spans start when `inlet` ran and end when `outlet` ran, so their
  duration is the turn as Open WebUI saw it, including the model call.
- Token counts come from the usage Open WebUI stores on the reply. For a
  streamed reply that requires the provider to send usage in the stream
  (enable the model's **Usage** capability for providers that only send it on
  request).
- `outlet` only queues the spans; a background thread exports them in
  batches, with a bounded queue. The chat does not wait for Future AGI, and
  if Future AGI is down the spans are dropped and the chat is unaffected.
  Neither hook raises: on any error they log a warning and return the body
  unchanged.

## Privacy

- With the default `redact=false`, the spans carry the conversation sent to
  the model and the reply, for every user of the Open WebUI instance. Set
  `redact=true` to keep message text out of Future AGI; ids, model and token
  counts are still sent.
- The user's email is never sent; `include_email_hash` adds only its SHA-256.
- The Future AGI keys travel only as request headers, never as span
  attributes.

## Limits

- **Failed turns are not traced.** When the model call fails, Open WebUI
  0.11.4 runs no `outlet`, so the filter sees no reply and sends nothing for
  that turn. Open WebUI's native OpenTelemetry export (below) records the
  failed request.
- Model ids are Open WebUI's, which may differ from the provider's model
  name.
- Each Open WebUI worker process exports on its own; spans still queued
  when a worker is killed (not stopped) are lost.

The native OpenTelemetry exporter in Open WebUI (`ENABLE_OTEL`) is a
separate path for its HTTP, database and Redis spans. Its spans are not
correlated with the spans this filter sends.

## Tests

From the repository root:

```bash
uv run --no-project --with pytest --with pydantic --with opentelemetry-sdk \
  --with opentelemetry-exporter-otlp-proto-http \
  pytest integrations/openwebui/tests/test_filter.py --noconftest -q
```

The tests call the hooks with the arguments Open WebUI 0.11.4 passes
(`body`, `__user__`, `__metadata__`; valves set on the instance before each
hook), check the frontmatter against Open WebUI's parser rules and the valves
against how it stores them, and export to an in-memory exporter or a
loopback OTLP/HTTP server. No Open WebUI and no network.
