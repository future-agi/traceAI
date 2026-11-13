# Zero-code auto-instrumentation contract

This directory is the cross-language contract for traceAI auto-instrumentation.
Python, TypeScript, Go, C#, Java and Ruby implement the same behaviour against the same
catalogs and golden fixtures, so a call traced in one SDK looks the same in every other.

- `wire-protocols.json`: catalog of AI wire protocols (providers and endpoints).
- `fixtures/*.json`: the 47 baseline golden request/response pairs.
- `fixtures/round6/*.json`: 293 additive A8/A10 contract goldens, including transport arrival offsets.
  SDK runners must adopt recursive discovery and timed-chunk replay to claim Round 6 parity.
- `privacy.json`: generated S6 credential-name, exclusion, signed-URL and media recognition catalog.
- `privacy-fixtures.json`: generated, SDK-neutral privacy input/output cases.
- `generate_privacy_fixtures.py`: canonical privacy catalog, deterministic generator and executable
  reference predicates. Edit `privacy_catalog()` and the cases here, never either generated JSON
  file; run without `--check` to regenerate both, or with `--check` to verify both read-only.
- `sync.py`: checks byte identity of all shipping catalog/privacy-bundle copies; `--write`
  refreshes existing copies, never adopts an SDK on its owner's behalf.

## Two capture layers

**Library layer.** Each SDK keeps its dedicated instrumentors (`traceai-openai`,
`@traceai/langchain`, ...). Auto-instrumentation enables every installed instrumentor whose
target library is present, without user code. Where the runtime allows it, enabling is lazy:
it happens when the target library is first imported, so unused libraries cost nothing at
startup.

**Wire layer.** A transport-level capture that recognizes AI API traffic by its wire format
(`/chat/completions`, `/v1/messages`, `:generateContent`, `/audio/transcriptions`,
`/v1/listen`, ...). It records any SDK, framework or hand-written client that speaks a known
protocol, including ones with no dedicated instrumentor and ones released after this SDK.
It runs on the HTTP clients each runtime uses for AI calls:

| SDK | Transports patched |
| --- | --- |
| Python | `httpx` and `httpx2` (sync and async), `requests`, `aiohttp` |
| TypeScript | `globalThis.fetch` as observed after preload; not direct `undici.fetch`/`undici.request`, `node-fetch`, or a fetch reference cached before preload |
| Go | `http.DefaultTransport` (and any `http.Client` built from it) |
| C# | `HttpClient` through a `DiagnosticListener` handler |
| Java | `java.net.http.HttpClient` and OkHttp when present |
| Ruby | `Net::HTTP` (and so Faraday's default adapter), `httpx` when present |

## Precedence and de-duplication (required)

1. A wire span is **not** created when the active span at request time was created by the
   library layer, meaning its `gen_ai.span.kind` is one of `LLM`, `EMBEDDING`, `RERANKER`,
   `GUARDRAIL`. The dedicated instrumentor already records that call with richer data.
2. A wire span is not created when OpenTelemetry instrumentation is suppressed in the
   current context (Python `_SUPPRESS_INSTRUMENTATION_KEY`, JS `suppressTracing`, and so on).
   Exporters run suppressed, so the SDK never traces its own export traffic.
3. While a wire capture is in flight, nested captures in the same context are skipped
   (for example a client that wraps another client).
4. Framework spans (`CHAIN`, `AGENT`, `TOOL`) do **not** suppress wire capture. The wire
   span becomes their child, which fills the gap when a framework traces steps but not model
   calls.

## Matching

1. Parse the URL. Resolve the provider from the host: exact `hosts`, then
   `host_suffixes`, then `host_prefixes`, then `ports`. When a provider sets
   `"match": "all"`, every listed host rule must hold. When no provider matches (a proxy,
   gateway or self-hosted server), the provider is the endpoint's `default_provider`
   (`anthropic_compatible`, `gemini_compatible`, `cohere_compatible`), else the catalog's
   `default_provider` (`openai_compatible`).
2. Walk `endpoints` in order; the first endpoint whose `methods` contains the method and
   whose `path` regex matches the URL path wins. `providers` restricts an endpoint to the
   listed providers; `exclude_providers` removes providers. Named groups (`model`, `voice`,
   `transcript_id`) are available to the parser.
3. Body validation is part of matching. Each format below lists the request keys it
   requires. When they are missing, or the body is not parseable, the call is not captured.
   This keeps the path patterns from claiming unrelated application routes. Two exceptions:
   - **Body unavailable** (a streaming or one-shot request body the hook cannot copy without
     consuming it, or a body larger than `FI_WIRE_MAX_BODY_BYTES`): the call is captured
     only when the host resolves to a catalog provider (not a default), with
     `traceai.wire.body_unavailable = true` (or `traceai.wire.truncated = true`) and no
     request-side content. Transport failures of such calls are captured the same way.
   - **Multipart** text fields are always read in full (each capped at 64 KiB); file parts
     are counted and never retained, whatever their size. So a large audio upload is never
     dropped for being large.
4. `only_when: "completed"` (AssemblyAI polling): create the span only when the response
   JSON has `status == "completed"` or `status == "error"`.

Spans are created when the response completes, with an explicit start time equal to the
request start. This lets a capture be dropped after the response is seen (rules 3 and 4
above) without emitting partial spans.

## Span shape

- **Name:** `"{operation} {model}"`, where model is the request model, else the response
  model; `"{operation}"` when neither is known.
- **Kind:** OpenTelemetry `CLIENT`.
- **Parent:** the active span at request time.
- **Status:** `ERROR` for transport failures and HTTP status >= 400, otherwise `OK`.
  The description is the provider's error message when the body has one (`error.message`,
  `error` string, or `message`), else `HTTP <status>`.

### Common attributes (every captured call)

| Attribute | Value |
| --- | --- |
| `gen_ai.span.kind` | endpoint `span_kind` |
| `gen_ai.operation.name` | endpoint `operation` |
| `gen_ai.provider.name` | resolved provider |
| `gen_ai.request.model` | request model (body, path group, or query `model`) |
| `gen_ai.response.model` | response model when present |
| `gen_ai.response.id` | response id when present |
| `gen_ai.response.finish_reasons` | string array when present |
| `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` / `gen_ai.usage.total_tokens` | inclusive ints when known; normalized by the A8 algebra below, never by summing cumulative snapshots |
| `gen_ai.usage.cache_read_tokens` / `gen_ai.usage.cache_write_tokens` | separate cache counters, already included in normalized input; ints when known |
| `gen_ai.usage.output_tokens.reasoning` | separate reasoning counter, already included in normalized output; int when known |
| `gen_ai.usage.provider_total_tokens` | raw provider total when it differs from the known normalized input + output |
| `gen_ai.request.temperature`, `gen_ai.request.top_p`, `gen_ai.request.max_tokens`, `gen_ai.request.seed`, `gen_ai.request.frequency_penalty`, `gen_ai.request.presence_penalty` | request params when present (`max_tokens` also reads `max_completion_tokens` and `max_output_tokens`) |
| `gen_ai.request.parameters` | JSON of the request body minus content keys (`messages`, `input`, `prompt`, `contents`, `system`, `systemInstruction`, `instructions`, `tools`, `documents`, `text`, `texts`, `inputs`, `query`, `transcript`, `file`, `audio_url`, `url`, `file_url`, `image_url`, `chat_history`, `message`, `preamble`, `content`, `requests`), after the privacy scrubbing below |
| `gen_ai.request.stream` | `true` when the response was an event stream (SSE or NDJSON) or the endpoint is a streaming variant (`:streamGenerateContent`, `:streamRawPredict`, `converse-stream`, `invoke-with-response-stream`), whatever the body framing (a Gemini JSON array counts) |
| `server.address`, `server.port` | from the URL |
| `http.request.method`, `http.response.status_code` | from the exchange |
| `traceai.capture.layer` | `"wire"` |
| `traceai.wire.endpoint` | endpoint `id` |
| `input.value`, `input.mime_type` | the request body as JSON (`application/json`), or the input text for TTS (`text/plain`) |
| `output.value`, `output.mime_type` | assistant text (`text/plain`) for chat formats; transcript text for STT; response JSON (`application/json`) otherwise |
| `gen_ai.response.time_to_first_chunk` | transport time-to-first-chunk: seconds from transport request handoff to first non-empty response-body chunk, streams only |
| `gen_ai.response.time_to_first_text` | time-to-first-text: seconds from transport request handoff to the arrival of the first non-empty text, reasoning, tool-call or audio delta, streams only |
| `traceai.capture.dropped_content_count` | number of content attributes omitted by the A7 projection; set when nonzero, reserved before content |
| `error.type` | HTTP status as a string, or exception class name, on errors |

### A8 inclusive token algebra (2026-10-30)

Normalize **once**, after merging raw provider usage. `I`, `O`, `T`, `R`, and `W` below mean
normalized input, output, total, cache-read and cache-write tokens:

| Provider/API | Input `I` | Output `O` | Details (never add twice) |
| --- | --- | --- | --- |
| Anthropic Messages and Anthropic-shaped Bedrock Invoke | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` | `output_tokens` | `R`/`W` retain their separate counters. `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` partition `W`; do not add them to `W` or `I` again. |
| Bedrock Converse | `inputTokens + cacheReadInputTokens + cacheWriteInputTokens` | `outputTokens` | `R`/`W` retain their separate counters. |
| OpenAI Chat/Completions/Responses | `prompt_tokens` / `input_tokens` | `completion_tokens` / `output_tokens` | Cached-token details are already in `I`; reasoning-token details are already in `O`. |
| Gemini GenerateContent (including Vertex) | `promptTokenCount + toolUsePromptTokenCount` | `candidatesTokenCount + thoughtsTokenCount` | `cachedContentTokenCount` is already in the prompt count; thoughts also populate the separate reasoning counter. |

When `I` and `O` are both known, **`T = I + O`**, even if a provider reports a different total.
Preserve a differing raw total as `gen_ai.usage.provider_total_tokens`. If either component is
unknown, retain a supplied provider total as `T` without inferring the missing component;
without a supplied total, omit `T`. This preserves input-only embedding usage with a reported
total, without manufacturing output tokens. Missing/null base counters are unknown, **not zero**;
explicit zero stays zero. An absent optional additive cache/tool-use/thought count contributes
zero to a known base count (provider schema default), but is not exported as a known detail.
An explicit null optional count is unknown and prevents computing that inclusive side. Detail
counts alone cannot establish a missing base count. Never estimate missing counts from text.

Usage events are cumulative **snapshots**, not token deltas: merge fields in arrival order,
last supplied value wins (including zero/null), retain fields omitted by a later partial
snapshot, and merge nested detail objects field-wise. Then normalize once. Repeated snapshots
must not increase usage; neither cumulative output nor TTL/cache/reasoning detail partitions
are added to their parent count. Content accumulation is independent of usage merging.

Merge **only complete frames in the observed transport prefix**, using the same chunk
partition and failure boundary as A10. Apply `chunk_sizes` before `error_after_chunks`;
the latter counts delivered transport chunks, **not SSE events**. One observed chunk may
complete several usage snapshots, all of which must merge in frame order. An incomplete
frame contributes no usage, even when its JSON payload is complete but its SSE blank-line
delimiter has not arrived. Neither future chunks nor their snapshots have been observed.
This boundary applies equally to **JSON-framed responses**, not only SSE: zero delivered
chunks or a lone `[` cannot contribute usage. A single JSON object contributes only after its
closing brace arrives. In a streamed top-level JSON array, each complete member object is a
frame, independently of the array's later comma/closing bracket; keep complete observed
members, but never extract usage fields from a partial member. Decode split UTF-8 codepoints
incrementally so a partial later member cannot hide already complete earlier members.

### A10 streaming milestones (2026-10-30)

Both milestones use the same monotonic start: the instant the request is handed to the
transport, before instrumentation validation/copying. Record each observed chunk's arrival time
**before** decoding/parsing. `time_to_first_chunk` measures the first non-empty body chunk,
even a role-only event, heartbeat or usage snapshot. `time_to_first_text` measures the first
non-empty model-output delta: visible text, reasoning/thinking, tool-call name/arguments, or
generated audio. Despite its historical name, it is not restricted to visible text. An ID,
role, empty string, empty tool scaffold, usage-only event, finish event or `[DONE]` is not output.
A tool block containing a non-empty name qualifies even before its arguments arrive. Audio is
recognized/countable without retaining raw payload; these milestones never mean played audio
or executed tools. Whitespace text is non-empty.

Qualifying representations (A10 recognition follow-up, 2026-10-31):

| Provider / protocol | Model-output payloads that qualify when non-empty |
| --- | --- |
| OpenAI Chat (including compatible endpoints) | Every `choices[].delta`: string `content`, `refusal`, `reasoning_content`, `reasoning`; `audio.transcript` or `audio.data`; `tool_calls[].function.name` / `.arguments`; legacy `function_call.name` / `.arguments`. Mistral list-valued `content` also qualifies through non-empty `text` parts or `thinking` parts' nested `thinking[].text` (nested type `text`). Check all choices and all parts, not only choice/part zero. |
| OpenAI Completions | `choices[].text`, directly on the choice rather than inside `delta`. |
| OpenAI Responses | String `delta` on `response.output_text.delta`, `response.refusal.delta`, `response.audio.transcript.delta`, `response.audio.delta`, `response.reasoning_text.delta`, `response.reasoning_summary_text.delta`, `response.function_call_arguments.delta`, `response.mcp_call_arguments.delta`, `response.code_interpreter_call_code.delta`, and `response.custom_tool_call_input.delta`. `response.output_item.added` also qualifies when a `function_call`, `tool_call`, `custom_tool_call` or `mcp_call` item supplies a name or arguments (including a nested `function`), a `custom_tool_call` supplies non-empty `input`, or a `code_interpreter_call` supplies non-empty `code`. Do not wait for later arguments, tool execution or ordinary text. |
| Anthropic | `content_block_delta.delta.text` (`text_delta`), `.thinking` (`thinking_delta`) and `.partial_json` (`input_json_delta`); `content_block_start.content_block` text/thinking or a `tool_use`, `server_tool_use` or `mcp_tool_use` name. Empty initial blocks do not qualify. |
| Gemini | `candidates[].content.parts[].text`, including text marked `thought: true`; `functionCall.name` or non-empty `functionCall.args`; `executableCode.code`; `inlineData` (`inline_data`) with an `audio/*` MIME type and non-empty `data`. The thought boolean, language/MIME metadata and empty argument objects alone are not output. |
| Bedrock Converse | Decoded `contentBlockDelta.delta.text`, `.reasoningContent.text`, and `.toolUse.input` / `.name`; `contentBlockStart.start.toolUse.name`. The synthetic event-payload fixtures establish recognition only, not AWS binary event-stream decoding. Anthropic Invoke payloads follow the Anthropic rules. |
| Cohere v2 | `content-start` / `content-delta` with non-empty `delta.message.content.text` or `.thinking` (also string/list content); a `type: "thinking"` tag alone does not qualify. `tool-call-start` / `tool-call-delta` function names or arguments in `delta.message.tool_calls` (object/list); `tool-plan-delta.delta.message.tool_plan`. |
| Cohere v1 | SSE or NDJSON `text-generation.text`; `tool-calls-chunk.text` (tool plan) and `tool-calls-chunk.tool_call_delta.name` / `.parameters` / `.text`; `tool-calls-generation.tool_calls[]` names or non-empty parameters. Count the first qualifying chunk; do not wait for the final `tool-calls-generation` event. |
| Ollama | Default NDJSON `/api/chat`: `message.content`, `message.thinking`, `message.tool_calls[].function.name` / `.arguments`. Default NDJSON `/api/generate`: string `response` or `thinking`. No explicit request `stream` flag is required. |
| Cartesia TTS | `type: "chunk"` event's non-empty `data` is generated audio; metadata and `done` events are not. |

Tool arguments may be string deltas or decoded non-empty objects where the provider uses
objects (Gemini, Ollama, Cohere v1); empty objects, IDs and nulls alone do not qualify.
Honor the SSE `event:` name when JSON omits its redundant event type. Audio recognition
never permits exporting raw audio or implies playback. The **earliest** qualifying event
wins, including a refusal or transcript at 30ms before ordinary text arriving at 70ms.
Role-only, empty/null-payload, usage-only and keep-alive events never establish first-text,
although their non-empty transport bytes can establish first-chunk. Hosted/server tool calls
follow the same rule as client-executed tool calls: model-generated names, arguments, input
and code qualify; tool lists, approval metadata, execution results (`codeExecutionResult`,
`functionResponse`, tool-result blocks), citations and completion notices do not establish
new model output. Recognition does not imply the tool ran or its output was consumed.

**NDJSON observation:** the framing dispatcher handles `response.ndjson` for any catalog
endpoint, not only Ollama. By default each compact UTF-8 JSON object plus `\n` is one chunk
with its own arrival offset. Apply `chunk_sizes` and then `error_after_chunks` before decoding.
A record qualifies only after its newline arrives; even a complete JSON object without its
newline is still a partial line. Buffer split UTF-8 bytes until the line completes. Multiple
lines in one chunk share that chunk's arrival time. A failure before the newline contributes
no output from that line; a failure after a complete output line preserves both milestones.
Empty transport chunks establish neither milestone, and an observed zero-millisecond arrival
is a known zero, not an unknown value.

The follow-up adds paired timed positive/empty-payload fixtures for these output forms,
plus NDJSON per-line, split-newline, split-UTF-8, coalesced and failure-prefix cases for
Ollama chat/generate and Cohere v1. Existing Gemini/Cartesia audio pairs remain in force.
The recognition/framing follow-up preserved its preceding **100 fixture files byte-for-byte**;
two additive timed copies of baseline `22-ollama-chat-stream.json` pin first-record output
and failure after that first record. The Cohere v1 `tool-calls-chunk` follow-up appends **34
fixtures (237–270)** without changing any of the preceding **236 fixture files**. Its matrix
covers top-level tool-plan text and delta name/parameters/text, including string and decoded
object parameters: timed positive/empty-null pairs, failure after output, earlier ordinary
text, empty scaffolds, plan/name before final generation, and NDJSON newline/UTF-8 partitions,
coalescing and failure prefixes. Existing files gaining arrival offsets: **none**.

The thinking/list-content/hosted-tool follow-up appends **70 fixtures (271–340)** while
preserving all preceding **270 fixture files byte-for-byte**. Fifteen representations cover
Cohere v2 thinking (object/list and content start), Mistral text/nested-thinking lists,
Responses MCP names/arguments, code-interpreter code and custom-tool input, Anthropic
server/MCP tool names, and Gemini executable code. Every representation has a positive,
empty/null/object/list-payload, failure-after-output and earlier-ordinary-text case; Gemini
code also has streamed JSON-array variants. Five structural-negative cases reject type/ID/
language scaffolds, empty parts and tool results; a Mistral later-choice/later-nested-part
case pins complete traversal. Existing files gaining arrival offsets: **none**.

**Current inventory (2026-10-31):** **340 wire fixtures = 47 baseline + 293 additive round6**;
**199 A8 projections** and **277 A10 arrival replays**. These counts supersede the historical
100/53/77/37 counts elsewhere in this document. Arrival replay covers **SSE, NDJSON and
streamed JSON arrays**. The Cohere recognition rows above also supplement the format
reference's shorter event list. These follow-ups do not weaken any privacy proof.

**A10 fixture scope / known limitation (2026-10-31):** round6 fixtures **157–166**
(`milestone-a10-bedrock-*-positive` / `-empty`) are **decoded-payload recognizer tests**,
not HTTP wire-conformance cases. Their SSE carrier is synthetic. SDK runners must route
these ten fixtures to a decoded-event recognizer harness, preserving each event's supplied
arrival timestamp, rather than send their artificial SSE bodies through a real
`converse-stream` wire endpoint. If that harness is unavailable, report these cases as
not applicable to the wire runner, not as passing binary decoding or failing wire conformance.
This is the explicit exception to the later instruction to run every round6 case through
an SDK wire layer; all other round6 fixtures remain wire cases. Real Bedrock binary
event-stream support remains request/status-only as stated in the format reference.

If an event spans chunks, use the arrival time of the chunk completing that event, not when a
worker eventually parses it. Several events in one chunk share its timestamp. Never substitute
parse completion, span end, or first-chunk time for unknown first-text time. If no qualifying
output is observed (including early close/failure), omit first-text rather than exporting zero.
Retain only bounded timestamps alongside the existing bounded capture; after loss of the event
arrival association, omit the affected milestone and set `traceai.wire.incomplete = true`.
Span end remains the last byte/error observation before parsing (R4-7); parsing latency is not
part of either milestone or span duration.

### Messages and tools

Messages are flattened with the existing traceAI keys:

- `gen_ai.input.messages.{i}.message.role`
- `gen_ai.input.messages.{i}.message.content`: text. Array content (OpenAI content parts,
  Anthropic and Bedrock content blocks, Gemini `parts`) concatenates the text parts of one
  message with `"\n"`. Non-text parts are skipped. Streamed deltas of one response are
  concatenated with no separator.
- `gen_ai.input.messages.{i}.message.tool_call_id` for tool results.
- `gen_ai.output.messages.{i}.message.role` / `.message.content`
- `gen_ai.output.messages.{i}.message.tool_calls.{k}.tool_call.id`,
  `.tool_call.function.name`, `.tool_call.function.arguments` (JSON string)
- `gen_ai.tool.definitions.{i}.tool.json_schema`: JSON of each request tool.

A system prompt that is outside `messages` (Anthropic `system`, Gemini `systemInstruction`,
Cohere v1 `preamble`) becomes input message 0 with role `system`.

### Format reference

| Format | Requires | Notes |
| --- | --- | --- |
| `openai.chat` | `messages` (or `prompt` for FIM) | Stream: SSE `chat.completion.chunk`, accumulate `delta.content` and `delta.tool_calls` by index; `usage` from the final chunk. |
| `openai.completions` | `prompt` | Output text is `choices[0].text`. |
| `openai.responses` | `input` or `model` (either one is enough: stored prompts send only `model` and `prompt`) | Input: string `input` becomes one user message; array items with `role` become messages. Output: `output[].content[].text` where type is `output_text`; `function_call` items become tool calls. Usage keys `input_tokens`/`output_tokens`. Stream: SSE; the `response.completed` event carries the full response. |
| `openai.embeddings` | `input` | `gen_ai.embeddings.dimension.count` = length of the first vector; `gen_ai.request.embedding_inputs_count` = number of inputs. Vectors are never recorded. |
| `openai.audio_transcription` | multipart field `model` | Request fields: `model`, `language` (`gen_ai.audio.language`), `prompt`, `response_format`. The file part sets `gen_ai.audio.mime_type` and `traceai.audio.input_bytes`; its bytes are never recorded. Output: JSON `text` (or the raw text body for `text`/`srt`/`vtt` formats) goes to `gen_ai.audio.transcript` and `output.value`. `duration` sets `gen_ai.audio.duration_secs`. |
| `openai.audio_speech` | `input` | `input.value` = text (`text/plain`), `gen_ai.audio.transcript` = text, `gen_ai.request.voice` = voice. Response is audio: `gen_ai.audio.mime_type` from content type, `traceai.audio.output_bytes` = byte count. Audio bytes are never buffered. |
| `openai.images` | `prompt` | `output.value` is the JSON with any `b64_json` values replaced by `"<base64 omitted>"`. |
| `openai.moderations` | `input` | `output.value` is the response JSON; `traceai.moderation.flagged` = any result flagged. |
| `rerank` | `query` and `documents` | `gen_ai.request.top_n`, `gen_ai.rerank.documents_count`; `output.value` = JSON array of `{index, relevance_score}`. |
| `anthropic.messages` | `messages` | Stream: SSE events `message_start` (id, model, input usage), `content_block_start` / `content_block_delta` (`text_delta`, `input_json_delta` for `tool_use`), `message_delta` (`stop_reason`, output usage). Usage follows A8: inclusive input adds the separate cache-read/cache-creation counts; nested TTL counts partition creation and are never added twice. Streaming usage snapshots merge before normalization. |
| `gemini.generate_content` | `contents` | Model from path group. Roles: `model` maps to `assistant`. Output text from `candidates[0].content.parts[].text`; `functionCall` parts become tool calls. Usage follows A8: prompt + tool-use prompt input, candidates + thoughts output; cache is already included. Stream: SSE (`alt=sse`) or a JSON array of responses; concatenate text and merge cumulative usage fields before normalizing once. |
| `gemini.embed_content` | `content` or `requests` | Dimension count from `embedding.values` or `embeddings[0].values`. |
| `cohere.chat` | `messages` (v2) or `message` (v1) | Output from `message.content[].text` (v2) or `text` (v1). Usage `usage.tokens.input_tokens/output_tokens` (v2) or `meta.billed_units`. Stream (v2 SSE): `message-start` (`id`), `content-delta` (`delta.message.content.text`), `tool-call-start`/`tool-call-delta`, `message-end` (`delta.finish_reason`, `delta.usage.tokens`); v1 stream events `text-generation` and `stream-end` (`finish_reason`, `response.meta`). |
| `cohere.embed` | `texts` or `inputs` | Dimension count when float embeddings are returned. |
| `bedrock.converse` | `messages` | Model from path (URL-decoded). Content blocks with `text`. Usage follows A8: `inputTokens + cacheReadInputTokens + cacheWriteInputTokens`, `outputTokens`; differing raw `totalTokens` stays separate. `converse-stream` uses binary event-stream: record request side and status only. |
| `bedrock.invoke` | any JSON body | Model from path. If the body has `messages`, parse it like `anthropic.messages`; otherwise `input.value` only. Response JSON is parsed as `anthropic.messages` when it has `content`, else recorded as `output.value` JSON. |
| `ollama.chat` | `messages` | Stream is NDJSON (default `stream: true`). Usage `prompt_eval_count` / `eval_count`; finish reason `done_reason`. |
| `ollama.generate` | `prompt` | Output `response`. |
| `ollama.embed` | `input` or `prompt` | Dimension count from `embeddings[0]` or `embedding`. |
| `deepgram.listen` | none | Model from query `model`, language from query `language`. Request body is audio, or JSON `{url}`. Transcript `results.channels[0].alternatives[0].transcript`; duration `metadata.duration`; `gen_ai.response.id` = `metadata.request_id`. |
| `deepgram.speak` | `text` | Model from query `model`. Response is audio, as for `openai.audio_speech`. |
| `elevenlabs.tts` | `text` | Model `model_id`; `gen_ai.request.voice` from path group. Response is audio (or JSON with `audio_base64` for `/with-timestamps`, which is recorded as byte count only). |
| `elevenlabs.stt` | multipart field `model_id` | Output `text`, language `language_code`. |
| `assemblyai.transcript` | none | POST: request JSON (`audio_url`, `speech_model`). GET: only when completed. Output `text`, duration `audio_duration`, language `language_code`, `gen_ai.response.id` = `id`. |
| `cartesia.tts` | `transcript` | Model `model_id`; response audio bytes, or SSE chunks whose `data` is base64 audio, counted as bytes. |

### Audio handling

Raw audio is never placed in attributes. SDKs record `gen_ai.audio.mime_type`,
`traceai.audio.input_bytes` / `traceai.audio.output_bytes`, `gen_ai.audio.duration_secs`,
`gen_ai.audio.language` and `gen_ai.audio.transcript` (the text side of STT or TTS).

`gen_ai.audio.transcript` is content and obeys the hide settings of its side: for speech to
text it is output (hidden by `FI_HIDE_OUTPUTS` or `FI_HIDE_OUTPUT_TEXT`), for text to speech it
is input (hidden by `FI_HIDE_INPUTS` or `FI_HIDE_INPUT_TEXT`). Hidden means the SDK's redacted
value, exactly as for `input.value` / `output.value`.

## Privacy (required)

### S6 single catalog

`privacy.json` is the authoritative recognition contract, not six independently maintained
lists. Each SDK loads it or generates **one credential-name predicate and one media predicate**
from it, and uses those same predicates for request headers/query/parsed body observation,
S5 harvesting, response/body JSON walking, export-time attribute keys, and URLs in any text.
Every SDK runs every case in `privacy-fixtures.json`. Recognition gaps are fixed once here;
a catalog rule applied inconsistently at different sites is an SDK bug.

The initial vocabulary is the union of the six SDKs' privacy implementations and missed names
reported in the round-4/5 verdicts. `credentials.sources` and `seed_names` retain the provenance;
`missed_name_sources` groups the missed names by review. All seed and missed names have generated cases.

1. **Credential patterns and exclusions.** ASCII-lowercase names and fold `-`, `_`, and `.`.
   Match the catalog's `contains` patterns (`token`, `secret`, `key`, `auth`, `password`,
   `passwd`, `credential`, `signature`, `sig`, `session`, `cookie`). Replace the **entire value**
   with `"<redacted>"`, regardless of JSON type. Exclusions are full-match regexes on the
   normalized name; every example is pinned by fixtures at all credential sites. They keep
   token limits/counts/usage/detail objects, explicit key identifiers, and named non-secret
   metadata. Wildcard metric exclusions have `unless_contains_patterns` guards: another
   credential marker in the **same component** wins (`client_secret_tokens`,
   `authorization_tokens`, `api_key_tokens`, `password_token_count`). They are not blanket
   exemptions for `usage.*` or `metric.*` subtrees. Both `*_tokens_details` and
   `*_token_details` (input/prompt/completion/output) preserve their objects and counters;
   `input_token_details.cache_read` is usage, but `input_token_details.api_key` and
   `client_secret_input_token_details` remain private. They are never harvested as credentials
   merely for containing `token`. Explicit ordinary-name exclusions include `author_name`,
   `keywords`, and `signal_strength`, pinned with text as well as numeric values.

   **Token timing/throughput (2026-11-01).** The guarded `token-timing` exclusion preserves
   `time_to_first_token`, `time_to_first_tokens`, `time_per_output_token`, `time_per_token`,
   `inter_token_latency`, `token_latency`, `tokens_per_second`, `output_tokens_per_second`,
   `ms_per_token` and `seconds_per_token`, including normalized dotted/flattened names such as
   `gen_ai.response.time_to_first_token` and `gen_ai.server.time_per_output_token`. Numeric
   values stay numbers, including fractional measurements. Every example and every guard is
   pinned at attribute, body, query and header sites. The same `unless_contains_patterns`
   guards as `tokens` retain independent credentials: `token`, `access_token`, `x-access-token`,
   `session_token`, `token_secret`, `api_token_latency_key` and `auth_token` remain private.
   Credential ancestors/descendants still win; timing names are not blanket subtree exemptions.

   `credentials.contextual_exclusions` remains empty. The reviewed name-only exception
   is instead the closed, versioned `logprob_tokens` list and an explicit parsed
   provider-response entry point, not a name/path heuristic available to every
   JSON payload. In that context, **logprob tokens are checked against registered secrets and credential shapes, not key-name rules**.
   Only `choices[*].logprobs.content[*].token` and
   `choices[*].logprobs.content[*].top_logprobs[*].token` are currently evidenced.
   Wildcards require actual arrays from that root. A nested tool output, user
   content string, arbitrary attribute payload or flattened key gets no exemption;
   independent credential ancestors/descendants remain private. No other provider
   path is inferred. `credentials.value_shapes` adds value checks independently
   of names; registered-value and media detection continue to run.

   Whenever value masking changes a choice's `message.content` or `delta.content`,
   mask that choice's **entire logprobs block**, including alternatives and bytes.
   For buffered streams, join by choice index and check individual and concatenated
   deltas before export. Mask all earlier buffered/current/later blocks for a tainted
   choice and its streaming text, preventing reconstruction across fragments.
   A redacted individual token also loses its byte-array alias. Node's staged
   OpenAI instrumentor exports once after buffering; it never exports a raw delta
   ahead of this scan. Generic shared fixtures retain their strict untrusted context;
   provider-root tests additionally exercise the explicit parser/export handoff.

   For dotted or bracket-indexed paths, test each nonnumeric component, retaining credential
   ancestors; also test the folded whole name unless a component is explicitly excluded.
   An excluded component cannot excuse a different credential component. Thus
   `gen_ai.usage.output_tokens.reasoning` stays numeric, `input.authorization.0` is private,
   and `usage.tokens.api_key` and `secret.input_tokens` are still private. Native nested
   maps/arrays, flattened keys, and indexed keys have equivalent meaning.

2. **Signed URLs.** Percent-decode parameter names once (query `+` means space), then use the
   same credential predicate **or** the explicit signed-URL parameter list. This includes
   AWS/Google signatures and credentials, Azure SAS fields, and returned links containing
   `client_secret`, `password`, `refresh_token`, or `api-key`. Scan URLs even inside prose.
   Isolate HTTP(S) URLs or relative `/path`/`?query` references in text, split off `#fragment`
   first, then split on the **first** `?`, `&` pairs, and each pair's first `=`. A later literal
   `?` is legal inside a parameter name or value: observation and direct export both recognize
   `client_secret?tag`, `password?foo`, and `token?filter`. Apostrophes are legal URI characters,
   **not** query token delimiters: `client_secret'tag`, `password'foo`, `token'filter`, and
   `token='private-value` must redact the entire value without leaving a suffix. Preserve
   enclosing single quotes in prose. Never treat fragment text such as
   `#fragment?token=ordinary` as a query. Keep original spelling, ordering, duplicate parameters,
   separators and fragments; replace only values with `<redacted>`. HTTP(S)
   `user:password@` becomes `<redacted>@`. Known-secret replacement still applies to every
   exported string, including fragments. The Azure short names are URL-only rules, not
   credential names in arbitrary JSON.

   Percent-decode each query value once and apply the shared media recognition, **including
   data URIs**, not just magic-byte/base64 detection. Thus a `blob=data%3Aaudio%2Fwav%3Bbase64%2C...`
   query is omitted just like its decoded scalar or body field; SVG and literal-payload data
   URIs follow the same rule. **Validate a whole captured URL before framing it.** A scalar,
   body field, attribute or array element beginning with HTTP(S), `/path` or `?query` is **not
   necessarily one URL**: response/exception messages can start with an ordinary URL and contain
   more URLs after prose. Scan every such URL independently, retaining the surrounding prose.
   Check the entire lexical boundary, not only the prefix. Recognized data-URI query values
   own their literal double quotes, whitespace, markup and backslashes: mask only those payloads
   to validate the URL boundary, then parse the **original** bounded URL. Never use credential
   or audio redaction to validate a boundary; redaction could swallow prose or later URLs.
   For example, `https://example.invalid/?blob=data:audio/basic,A"payload&model=keep#anchor`
   becomes `https://example.invalid/?blob=<omitted: data URI>&model=keep#anchor`, with no suffix
   left outside the query parser. Prose uses delimiter-limited scanning even inside bounded fields.
   Frame the URL's query pairs before scanning literal data URIs:
   following `&model=keep` and `#anchor` are not media payload. A decoded value beginning with
   `data:` has an exact query-value boundary, so decoded SVG brackets, quotes or whitespace
   belong to its payload, not to surrounding prose. Data URI payload decoding uses `unquote`
   semantics (retain base64 `+`) after the outer query's normal plus-as-space decoding. Leave
   unchanged values in their original encoding and do not recursively decode nested URLs.

   Treat decoded query pairs as **one flat object** for media rules. Collect MIME/type hints
   before scrubbing values, so `mime_type=audio/pcm&content=AQIDBA==`,
   `parts.0.mime_type=audio/pcm&parts.0.content.chunk=AQIDBA==`, and
   `type=input_audio&data=AQIDBA==` behave like native/flattened objects. Hints bind to the
   exact indexed parent and do not cross metadata boundaries; the order of pairs is irrelevant.
   Duplicate hints combine (a later `text/plain` does not erase `audio/pcm`); never collapse,
   reorder, or re-encode original query pairs. Parameters such as `audio.voice=alloy` and
   `audio.max_tokens=5` remain metadata, while `audio.chunk` and `audio.0` are payload.

3. **Structural media, independent of key/path/size.** Check the decoded base64 **prefix**
   (at most 32 bytes; never decode the complete media) for PNG, JPEG, GIF87a/GIF89a,
   RIFF **and** WAVE/WEBP at offset 8, ID3, MP3 frame sync, OggS, fLaC, ISO-BMFF `ftyp` at
   offset 4, and PDF. `media.magic` specifies offset/hex tests and optional bit masks;
   all tests within a rule must match. URL-safe, unpadded, and CR/LF-wrapped base64 are
   supported. Recognized media under **any key** is omitted, even below 256 characters.
   A RIFF header alone, or `ftyp` at the wrong offset, does not match.

   A `data:` URI is omitted in any captured text, whether base64, percent-encoded, or literal;
   count percent-decoded base64 payload characters without translating `+` to space.
   Base64 replacement is `"<omitted: N base64 chars>"`; other encodings use
   `"<omitted: data URI>"`. Require a scheme boundary: the preceding character cannot be an
   ASCII letter/digit, `+`, `.`, `-`, `_`, or `:`. Ordinary prose `metadata:source,year`
   and names containing a `data:` suffix are retained, as are `data:` prefixes without a
   comma. Scanners must not repeatedly rescan suffixes on malformed prefixes.

   Use an **exact value boundary** for scalars, body fields, array elements, attributes and
   decoded query values: when the whole value begins with `data:` and has a comma, omit it
   completely **before** contextual media or URL scanning. Literal quotes, apostrophes, SVG
   markup, whitespace, `&`/`#`, and embedded URLs are payload, not delimiters. Thus
   `data:audio/basic,A'SYNTHETIC_MEDIA_20261030` and single-/double-quoted literal SVG data
   URIs become one `<omitted: data URI>` without retaining a suffix. Only **free text** uses
   delimiter-limited scanning; `data:audio/basic,literal after` in prose preserves ` after`,
   whereas that exact scalar value is entirely omitted.

4. **Contextual media and audio.** Normalized MIME sibling names (`mimeType`, `mime_type`,
   `mediaType`, `media_type`, `contentType`, `content_type`, `content-type`) recognize
   audio/image/video, PDF and binary MIME types. Their payload-field list includes **content**,
   not just `data`/`bytes`. Context survives nested `source`/`inlineData` wrappers and arbitrary
   payload descendants such as `content.chunk` and `input_audio.data.chunk`; checking only the
   terminal field is insufficient. Flattened siblings bind to their **exact indexed parent**,
   never to a neighboring part. Scalar, string-array and indexed MIME hints all bind to the
   MIME field's object (`parts.0.mime_type.0` describes `parts.0`, not the MIME value's index).

   Audio fields/type hints and their payload descendants have **no minimum length**, including
   `input.audio.0`, `input.audio.data.0`, `audioContent`/`audio_content`, short PCM and numeric
   byte arrays. An audio field's direct scalar/indexed/array samples are payload, but an
   **audio object establishes audio context, not blanket payload ancestry for every child**.
   Only explicit payload fields (`data`, `content`, `bytes`, `b64`, `b64_json`, `delta`, `chunk`,
   `samples`, and the catalog's existing `value`/`body`/`base64` forms) start payload ancestry
   through arbitrary wrappers. Direct-sample context is **separate from established payload
   ancestry**: nested audio fields must never clear the latter. Thus `audio.data.audio.source.0`
   and `audio.data.audioContent.source.0` remain payload, while bare `audio.source.0` is unmarked.
   A MIME-identified `audio_base64` is both an audio field and a MIME payload field: its
   `source.0` descendants remain payload even though a bare audio object would not mark named
   children. These rules apply identically to native objects, flattened/indexed attributes and
   query parameters; only metadata boundaries stop established ancestry.
   `type=input_audio` similarly establishes context, not payload.
   Payload strings use the base64 marker when in the catalog alphabet, otherwise
   `"<omitted: media>"`; numeric/bool payload scalars or arrays use that latter marker.
   Arrays of strings/objects retain structure. Preserve voice, format, max_tokens, sample_rate,
   speed, IDs, expiration timestamps, usage, transcript, language, duration, MIME, URL,
   byte-count metadata and unmarked parameters unless another privacy/hide rule applies. `audio.metadata_fields` are **boundaries**: clear inherited
   media/audio/payload context at that component, preserving `audio.transcript.content` and
   MIME-adjacent `transcript.content` as text. A fresh explicit MIME hint/audio field can start
   a new scope below a metadata boundary; credential recognition is never disabled there.
   Raw audio is never exported.

   The `>256` threshold applies **only to unrecognized base64** in the catalog's generic media
   fields (including flattened/indexed representations). It never exempts magic-recognized,
   MIME-identified or audio payloads. Long unrelated base64 with no magic or media marker is
   not classified as media merely for being long. Counts include original CR/LF characters.

### S1 boundary and S5 known secrets

All agent-exported spans, including forwarded application/library spans, pass through a
single sanitizer immediately before serialization on a detached snapshot. Apply catalog
recognition and hide flags to attributes/arrays, status, events, links, resource, scope and
tracestate, not just wire content. Never invoke unknown-object stringifiers/getters; bound
work and fail closed on errors, retaining identity/timing and dropping content. Other
exporters must continue to see the original span.

Transport hooks observe outgoing header/query credential values **even when S4 suppresses
wire-span creation**; they do not buffer suppressed request bodies. Harvest body credentials
only from data already parsed. The process-wide S5 registry holds at most 1024 values of at
least eight characters (LRU), seeded with FI keys and credential-like environment values at
initialization. Every exported string replaces registered values longest first. Integrations
register extra credentials through the core API, never per-trace credential inheritance.
Name-based redaction itself has no eight-character minimum.

### Privacy fixture format and synchronization

`generate_privacy_fixtures.py` creates `privacy.json` from `privacy_catalog()` and the single
`privacy-fixtures.json` bundle. **Edit the catalog source and cases in the generator, never
either generated JSON file.** The fixture bundle contains `version`, `catalog_version`, and
`cases`. Each case has a unique `name`, `covers` rule IDs, `input`, and exact JSON `expected`:

```json
{
  "name": "example-indexed-audio",
  "covers": ["audio-field:audio"],
  "input": {"site": "indexed_paths", "path": "input.audio.0", "value": "QQ=="},
  "expected": "<omitted: 4 base64 chars>"
}
```

`site` identifies the SDK entry point to exercise; `path` supplies a structural/attribute key
(or empty for a root value); `value` is JSON. Optional `siblings` maps **full flattened keys**
to MIME/type hints. Optional `known_secrets` seeds synthetic S5 values for an echo case.
`expected` preserves JSON types/array order. Compare exactly; do not normalize omission markers
or silently skip unknown sites. The sites are request headers/query/body, response bodies,
attribute keys, flattened/indexed paths, MIME siblings, arbitrary-key magic bytes, and URLs
inside free text. Exact scalar/body/attribute/query values are framed; `free_text_urls` is
explicitly unframed and uses delimiter-limited scanning (the reference's `framed=False`).
Nested fields and array elements still have their own exact boundaries. Where transport
observation cannot itself return scrubbed output, observe
at that site and verify the resulting export with the same boundary; do not change what the
application sends/receives. The generator's reference implementation establishes recognition,
not SDK registry lifecycle, transport coverage, hide flags, or malformed-JSON policy.

`--check` validates reference output against independently constructed golden expectations,
idempotency, catalog coverage, and exact generated bytes without writing. It also walks every
body/header/URL representation in all **47 wire fixtures**: only six explicitly allowlisted
security leaves may change; every other leaf and every exact expected attribute/name/status
must survive. This pins token-usage fields, including provider camelCase counts and detail
objects, against false positives. A new wire case requires explicit review of this proof.

Adopt by adding `privacy.json` and `privacy-fixtures.json` beside the SDK's wire catalog;
existing copies, including launcher-embedded runtime copies, must be byte-identical. `sync.py`
reports missing privacy files at each canonical shipping destination **and beside every
additional discovered wire catalog**, including launcher-embedded runtimes, as **pending,
non-blocking**, but any stale existing copy fails immediately. Test/data-only copies never
satisfy shipping adoption. Main flips `REQUIRE_PRIVACY_COPIES` to `True` after adoption; CI can
already enforce it with `--require-privacy-copies`, which fails for each missing adjacent file.
`--write` updates existing files only.
It must not be run while other SDK owners are editing those paths. Build outputs, virtualenvs,
caches and scratch checkouts are excluded, with directories pruned before traversal.

```sh
python3 -I spec/auto-instrumentation/generate_fixtures.py --check
python3 -I spec/auto-instrumentation/generate_privacy_fixtures.py --check
python3 -I -B spec/auto-instrumentation/test_privacy_spec.py
python3 -I spec/auto-instrumentation/sync.py
```

On shared hosts, prefix **every** generator/test/runtime invocation with the host-safety
wrapper that the host provides, if it has one.

### Review follow-up (2026-10-30)

The preceding three S6/B2 catalog blockers are pinned by generated cases and named regressions
in `test_privacy_spec.py`: same-component credential/metric precedence and contextual ordinary
names; payload-descendant ancestry with metadata boundaries; and query observation/export
parity for literal `?` names with fragment preservation. Both preceding OTHER findings were fixed,
not accepted as ongoing limitations: `metadata:source,year` is retained by a scheme-boundary
negative fixture, and strict sync rejects testdata-only adoption and missing embedded bundles.

The subsequent two B2 blockers are also fixed: URL tokenization retains legal apostrophes in
credential query names/values, and query values use the shared data-URI recognition after
percent-decoding. Generated `url-apostrophes` and `data-uri-query` cases test request URLs,
returned URL objects, free text and attribute values **without registry seeds**. Named
regressions cover observation/export parity, credential suffixes, surrounding quotes, encoded
and literal data URIs, parameter/fragment preservation, boundary negatives and nonrecursive
decoding.

The final S6 consistency follow-up closes both remaining catalog/reference B2 blockers:
whole-value data URI framing at every bounded value site (including literal quoted SVG/audio),
and query-wide MIME/type sibling context with indexed isolation. The ordinary-usage/audio
metadata OTHER finding is **fixed**, not accepted: singular/plural token-detail objects and
counters survive, and audio objects omit only explicit payloads/indexed samples, not parameters.
Generated coverage pins `data-uri-value-boundaries`, `data-uri-free-text`, `query-media-object`,
`token-details-objects`, and `audio-object-payload-scope`. Named regressions additionally check
query hint ordering/duplicates, nonrecursive decoding, metadata boundaries and independent
credential/magic/data-URI recognition.

The subsequent S6b verification found and fixed two remaining B2 consistency defects
(2026-10-30): a nested audio field could erase already-established payload ancestry, and the
prose URL scanner could truncate an exact captured URL before its query values were framed.
The fixes separate local audio-sample context from persistent payload ancestry and parse
bounded URL values before delimiter scanning. Six named regressions and **474 additional
golden cases** cover audio spellings, native/flattened/indexed/query sites, MIME/audio-field
overlap, numeric samples, metadata boundaries, literal quoted/whitespace/SVG query payloads,
relative URLs and intentional free-text delimiter handling. New mandatory coverage IDs are
`payload-nested-audio`, `mime-audio-payload-overlap` and `url-value-boundaries`.

The next S6b verification found and fixed the URL-prefix shortcut regression (2026-10-30):
exact outer value boundaries do not prove a message is one URL. Whole-URL lexical validation
now falls back to scanning every URL in prose, while recognizing literal data-URI query
payload boundaries before that validation. Four named regressions and **392 additional golden
cases** pin later credential/audio/data-URI URLs after fragment-bearing or ordinary-query URLs,
absolute and relative prefixes, whitespace/markup separators, response/exception/array sites,
MIME/type query siblings, idempotency and surrounding-prose preservation. New mandatory coverage
is `url-prefixed-prose`; no registry seeds are needed.
At this 2026-10-30 checkpoint, the suite had **56 tests** and **4,985 golden cases**, each checked
again for idempotency; **216 rules/sites** were pinned. The unchanged
47-wire-fixture proof still retains 743 request/response leaves (except its six intentional
redactions), 291 expected values and 45 token metrics. No SDK files are updated by this follow-up.

### Known limitations (2026-10-30)

- **Staged SDK adoption (2026-10-30):** this spec/generator does not make an SDK compliant until
  its owner installs both files, unifies its predicates and runs the complete privacy bundle.
- **Free-text framing (2026-10-30):** only prose uses delimiter-limited URI scanning. Literal
  whitespace/quotes/markup in an unescaped URI embedded in prose are ambiguous delimiters;
  the scanner cannot reconstruct an arbitrary literal payload beyond them. Percent-encode
  such free-text payloads or use the hide controls below. This does not restrict exact
  scalar/body/attribute/array-element/decoded-query data URI values, which are omitted whole,
  **nor exact captured URL values**: those are parsed before delimiter scanning, including
  literal quoted/whitespace/SVG data-URI query payloads. A URL prefix alone does not establish
  one exact URL: ordinary multi-URL prose inside a bounded field is scanned URL by URL and is
  **not** covered by this ambiguity. No new limitation is accepted for the S6b blockers
  (2026-10-30); all have regression tests and generated fixtures.
- Name/magic recognition is intentionally structural and heuristic, not arbitrary secret or
  media inference. The no-false-positive proof is over the pinned fixtures, not every possible
  application schema; new non-secret names need explicit exclusions and fixtures. The generic
  base64 fallback is restricted to media fields. Use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`,
  `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`, and
  `FI_HIDE_LLM_INVOCATION_PARAMETERS` for sensitive application data beyond these markers.
- Application-transformed secrets (splitting, numeric retyping, opaque JSON-in-string,
  extracting a signature into prose, or re-encoding), unmarked credentials that never cross
  an instrumented transport, and manual paths lacking agent transport hooks remain ROUND5
  B2 OTHER limitations. Use the hide controls above or explicitly register known secrets.
  The S6 reference is not a replacement for S1's SDK-specific safe object/snapshot handling.

## Safety limits

### A7 deterministic attribute projection (2026-10-30)

Projection happens **before** attributes enter the SDK's bounded attribute container, not
only at export when SDK eviction has already lost usage. Resolve the effective span attribute
count limit from the actual provider (`OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT`, general SDK limit,
or provider configuration; default **128**). A host may set a smaller limit; do not raise it
or replace its provider. Attribute-value/body limits still apply independently. Project the
sanitized, bounded candidate set, without mutating an application-owned span/export view.

1. Reserve capacity for **all protected attributes** before admitting any content: identity
   (`gen_ai.span.kind`, response ID, FI project routing), outcome (`error.type`, HTTP status,
   finish reasons), provider/model/operation, every known `gen_ai.usage.*` counter, **TIMING**
   (the exact keys below, when present), provenance
   (`traceai.capture.layer`, `traceai.wire.endpoint`, endpoint/method metadata), and degradation
   markers (`traceai.wire.truncated`, `.incomplete`, `.body_unavailable`,
   `.undecoded_encoding`, and `traceai.capture.dropped_content_count`). Preserve span name,
   trace/span/parent IDs, kind, status/description and timestamps as structural span fields;
   they do not consume attribute slots. Unknown usage or timing is not invented to fill a slot.

   **Protected TIMING family (2026-11-01):** `gen_ai.response.latency`,
   `gen_ai.response.time_to_first_chunk`, `gen_ai.response.time_to_first_text`,
   `gen_ai.response.time_to_first_token`, `gen_ai.server.time_to_first_token`,
   `gen_ai.server.time_per_output_token`, and `gen_ai.server.request.duration`.
   Reserve each present key before content, not as optional noncontent metadata. Protection
   does not change measurement units or the A10 milestone definitions, and does not bypass S1.
2. Reserve **one slot for the dropped-content count whenever content will be omitted**; compute
   the final selection with that reservation before writing anything. The count is the number
   of candidate **content attribute keys** not emitted, not bytes, tokens or messages. Include
   raw JSON/text values, content MIME companions, flattened messages, tool definitions,
   transcripts and invocation-parameter content in this accounting. Emit no count when zero.
   Pre-existing `traceai.capture.dropped_content_count` values accumulate rather than being
   reset by projection; do not mix in OTel's distinct all-attribute drop counter.
3. Within the remaining budget, prefer bounded **output** over input: output message/tool
   groups first, then output value/transcript representations, then input message groups
   **newest first**, then input transcripts/tool definitions/parameters and aggregate input.
   Groups are atomic (role/content/tool-call fields and their applicable MIME companion): do
   not emit a dangling partial message or malformed/truncated JSON. Preserve original message
   indices; do not renumber. For equal-priority output groups use ascending numeric message/
   tool indices, then attribute-key lexical order; input-message order is descending numeric
   index. Fit each group only when the whole group fits. Retained input messages must be a
   contiguous newest suffix: once a newer input group cannot fit, omit it and all older
   groups, never fill the gap with a smaller old message. Do not admit input while a
   higher-priority output group is being omitted for capacity. Thus the oldest input messages
   are dropped first, and output wins whenever the host capacity can hold it. If aggregate
   `input.value` duplicates a dropped message, omit it too, rather than accidentally restoring
   the evicted conversation.
   Optional noncontent metadata fits only after protected fields and bounded output/input.
4. If even the protected set exceeds the host's limit, admit **no content**. Order protected
   families deterministically: degradation/drop markers, identity, outcome, provider/model/
   operation, usage, **TIMING**, provenance; use lexical key order inside each family. Retain the prefix
   that fits. A zero-slot limit cannot preserve an attribute marker; report that impossibility
   via bounded out-of-band diagnostics/counters, never by increasing the host limit. No
   implementation may claim to retain more protected fields than the configured limit allows.

The same candidates and limit must always select the same attributes regardless of map
insertion order or SDK first-/last-write eviction policy. Verify both default 128 and smaller
host limits, including no-content and protected-set-overflow cases. Export-time S1 scrubbing
remains authoritative even for projected identity/metadata.

- Request and response bodies are buffered only up to `FI_WIRE_MAX_BODY_BYTES`
  (default 4 MiB). Past the limit, capture keeps the span but stops parsing and sets
  `traceai.wire.truncated = true`. Audio and other binary responses are counted, not buffered.
- **Compressed bodies.** When the hook sees encoded bytes (for example an httpx stream
  before decoding), the SDK decodes `gzip` and `deflate` (and `br`/`zstd` when the runtime has
  a decoder) before parsing. When it cannot decode, the span keeps request-side data and
  sets `traceai.wire.undecoded_encoding` to the encoding.
- **Failures mid-stream.** A transport error after the response started (timeout, reset)
  ends the span with status `ERROR`, `error.type` set to the error's type name, and whatever
  output was parsed so far. A consumer that stops reading early and closes the response is
  not an error: the span is `OK` with the partial output and `traceai.wire.incomplete = true`.
  An exception raised by the application's own code while it reads the body is a consumer
  abort, not a transport failure. A response read to its end (EOF, or exactly its
  `Content-Length`) is complete, however the application closes it.
- **Export-time privacy.** The S1 Privacy boundary and hide flags cover every span the agent
  exports, including forwarded application/library spans: attributes, status, events, links,
  resources, instrumentation scopes and tracestate, on detached snapshots.
- **Bounded work.** Instrumentation never adds latency before the request is sent: a request
  body that cannot be copied without blocking (a stream, a pipe, a slow replay) is treated as
  unavailable rather than read ahead. Shutdown and flush honour their deadlines.
- Instrumentation never changes what the application sees: it does not consume streams the
  application reads, alter headers, retry, or raise. Any internal error is logged at debug
  level and the call proceeds untouched.
- Every attribute goes through the SDK's `TraceConfig` masking (`FI_HIDE_INPUTS`,
  `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, ...), the same as library-layer spans.
- Context attributes (`using_session`, `using_user`, `using_metadata`, ...) are added to wire
  spans the same way library instrumentors add them.

## Environment variables (all SDKs)

| Variable | Default | Meaning |
| --- | --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | none | Optional FI exporter credentials, resolved as an atomic pair per source (below). Not required to instrument with an application-supplied provider, exporter or processor. Without a destination and without a complete FI pair, skip FI exporter setup, warn once and leave the application untouched. |
| `FI_BASE_URL` | `https://api.futureagi.com` | Collector base URL. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, else the working directory's name, else `DEFAULT_PROJECT_NAME` | FI routing project, recorded separately as `project_name`; never assigns or replaces the application's `service.name` or resource identity (A6). |
| `FI_PROJECT_TYPE` | `observe` | `observe` or `experiment` for the zero-code path. |
| `FI_AUTO_INSTRUMENT` | unset | Python only: `true` instruments every interpreter in the environment, with no command change. |
| `FI_INSTRUMENTATIONS` | all | Comma-separated allowlist of integration names. |
| `FI_DISABLED_INSTRUMENTATIONS` | none | Comma-separated denylist. `wire` disables the wire layer. |
| `FI_WIRE_CAPTURE` | `true` | `false` disables the wire layer. |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304` | Body buffering limit. |
| `FI_AUTO_DEBUG` | `false` | Log the per-integration report at startup. |

| `FI_SAMPLE_RATE` | `1.0` | Head sampling ratio, parent-based. |
| `FI_CONFIG_FILE` | `./futureagi.toml` | Agent config file, see below. |

`OTEL_SDK_DISABLED=true` turns everything off, as in upstream OpenTelemetry.

## Agent behaviour

The zero-code entry points behave like an APM agent: install and run with an application-owned
OpenTelemetry destination, or optionally set a complete FI credential pair for FI export.
Content capture remains **ON by default**, subject to the same hide controls on every route.

- **Neutral activation (A11, 2026-10-30).** An application-supplied SDK tracer provider,
  exporter **or** span processor is sufficient to activate the library and wire instrumentors,
  with no FI keys. A supplied provider is reused; a supplied exporter/processor without a
  provider is attached to the agent-created provider. Do not construct an FI exporter, resolve
  an FI collector connection, test FI authentication, or warn about absent FI keys in this
  mode. A complete FI pair adds an FI destination only when that route is requested. With
  neither an application destination nor a complete FI pair, skip exporter setup and warn
  once; do not install a replacement provider or disturb existing application behaviour.
  `OTEL_SDK_DISABLED=true` and explicit instrumentation disables still win.
- **Fail open.** An unreachable collector, a broken instrumentor or an unsupported library
  version logs one warning and leaves the application running untouched. A missing/partial
  FI pair never disables an application-owned destination. Nothing on the startup path makes
  a network call or raises.
- **No signal-handler takeover.** The agent never replaces how the application handles
  SIGINT/SIGTERM. It flushes on normal exit (`atexit`, `beforeExit`, `AppDomain.ProcessExit`,
  shutdown hook) and exposes an explicit `shutdown()`/`flush()` for short-lived processes.
  Where a SIGTERM kills the process without running exit hooks (Python and Node: servers such
  as uvicorn re-raise SIGTERM after their own graceful shutdown), the agent chains in front of
  the existing SIGTERM handler: it flushes (bounded, 5 s), then runs the previous handler, or
  restores the default action and re-raises, so the exit code is unchanged.
- **Coexists with OpenTelemetry.** Reuse an application's global SDK tracer provider; add
  only explicitly configured destinations and never replace existing processors/exporters.
  When no provider exists, the agent may install one for the supplied destination or FI pair.
  Initialization is once per process (S2); repeated initialization cannot rewire ownership.
- **Resource identity (A6, 2026-10-30).** Preserve every application resource attribute,
  including `service.name`, `service.namespace`, `service.instance.id`, deployment and cloud
  identity; merging traceAI defaults must not drop unknown application keys. If the application
  supplied `service.name`, keep it exactly, even when `FI_PROJECT_NAME` differs. With an
  agent-owned provider and no application service name, use normal OTel resource detection,
  `OTEL_SERVICE_NAME`/`OTEL_RESOURCE_ATTRIBUTES`, and OTel fallback semantics, never the FI
  project as a service-name fallback. FI routing is the separate `project_name` attribute
  (and existing project-type/version metadata). Add routing on the FI-owned export snapshot
  when the provider is host-owned; do not mutate its resource or other exporters' view.
  Resource preservation does not bypass S1 credential/media scrubbing of the FI export view.
- **Transactions.** Incoming HTTP requests become root `SERVER` spans when a server
  instrumentation is available for the framework in use (FastAPI, Flask, Django, Express,
  ASP.NET Core, Spring), so every AI call is nested under the request that caused it.
  Outgoing non-AI HTTP calls are not traced by default.
- **Report.** At startup, with `FI_AUTO_DEBUG=true`, and on demand through a `doctor`
  command, the agent prints the status of every integration (see statuses above), the
  resolved configuration with secrets masked, and whether the collector accepted a test
  export.
- **Bootstrap.** A `bootstrap` command lists the libraries installed in the environment and
  the instrumentation packages that cover them, and installs those packages on request.

### Config file

`futureagi.toml` (or the path in `FI_CONFIG_FILE`) uses a flat TOML subset that every SDK can
parse without a TOML library: one `key = value` per line, values are `"strings"`, `true` /
`false`, integers, floats, or single-line arrays of strings; `#` starts a comment; `[tables]`
are ignored. Each key maps to the variable `FI_<KEY upper-cased>` (`debug` maps to
`FI_AUTO_DEBUG`). Noncredential precedence: arguments in code, then environment, then the
file, then defaults. Keep secrets in the environment; the file is meant to be committed.

**Atomic FI credentials (ROUND5 S7-pre, 2026-10-30).** Resolve `FI_API_KEY` and
`FI_SECRET_KEY` as **one pair per source**, in order: explicit arguments > environment >
config file > fi profile. Select the first complete, nonempty pair. A source declaring just
one usable half is ignored as a whole, with one warning naming the missing half and source,
never the values; continue to the next source. Never combine a key from one source with a
secret from another. For example, an environment API key alone plus a complete TOML pair
selects **both TOML values**, not the environment key/TOML secret. A complete explicit pair
beats a complete environment pair; absent sources do not warn. A partial pair does not disable
neutral activation. The process-wide S5 registry still protects every known FI credential
value present at initialization, including ignored halves; diagnostics never reveal them.

```toml
project_name = "checkout-agent"
project_type = "observe"
instrumentations = ["openai", "langchain", "wire"]
disabled_instrumentations = ["requests"]
wire_capture = true
sample_rate = 0.25
debug = false
```

## Zero-code entry points

| SDK | No code change | One line |
| --- | --- | --- |
| Python | `fi-instrument python app.py`, or `FI_AUTO_INSTRUMENT=true python app.py`, or `opentelemetry-instrument python app.py` | `fi_instrumentation.init()` |
| TypeScript | `node --import @traceai/auto/register app.js` (also `NODE_OPTIONS`, `-r @traceai/auto/register` for CJS) | `import { init } from "@traceai/auto"; init()` |
| Go | blank import `_ "github.com/future-agi/traceAI/go/auto"` | `auto.Init(ctx)` |
| C# | `DOTNET_STARTUP_HOOKS=/path/TraceAI.AutoInstrumentation.dll` | `TraceAIAuto.Init()` |
| Java | `java -javaagent:traceai-javaagent.jar -jar app.jar` | `TraceAIAuto.init()` |
| Ruby | `RUBYOPT=-rtraceai/auto ruby app.rb` | `require "traceai/auto"` |

Each entry point reports, per integration, one of: `instrumented`, `pending` (library not
imported yet), `not_installed`, `missing_instrumentor` (library present, dedicated package
absent; the wire layer still covers it when the protocol is known), `disabled`, `manual`
(needs code, with a reason), or `failed` (with the error).

## Fixtures

Each `fixtures/**/*.json` file holds one case. They are generated by `generate_fixtures.py`;
change a case there and regenerate, never edit the JSON by hand. There are **340 cases**:
47 baseline files at the directory root and 293 additive Round 6 files under `round6/`.
The namespace preserves S6's frozen 47-fixture privacy proof and existing SDK baseline runners
without editing privacy files. It is **not** permission to skip new cases: SDK owners must
use recursive discovery and implement the arrival replay below for Round 6 conformance.
The generator checks bytes recursively, validates A8 usage on 77 captured cases, replays all
37 timed A10 cases, and independently checks all 53 new fixtures against S6 recognition
(96 expected token metrics preserved; only four explicitly allowlisted synthetic/empty audio
payload leaves are omitted). Golden usage/milestone numbers are supplied independently, not
calculated from the oracle's output.

**Complete baseline numeric change list (A8, 2026-10-30):**

| Fixture | Expected attribute | Before | After |
| --- | --- | --- | --- |
| `15-anthropic-messages-basic.json` | `gen_ai.usage.input_tokens` | 15 | 135 |
| `15-anthropic-messages-basic.json` | `gen_ai.usage.total_tokens` | 19 | 139 |
| `17-gemini-generate-content.json` | `gen_ai.usage.output_tokens` | 5 | 13 |

No request/response numbers or other expectations in the original 47 cases change. In
particular, OpenAI cached/reasoning accounting is unchanged; the original Gemini total
remains 25. Positive Responses-cache coverage is additive, not a rewrite of fixture 09.

Named A8 goldens cover `anthropic-stream-cache-ttl-snapshots`,
`bedrock-converse-cache-provider-total`, `bedrock-invoke-anthropic-cache`,
`gemini-cache-tool-use-thoughts`, `gemini-stream-cumulative-cache-thoughts`,
`openai-responses-cached-tokens`, `openai-responses-cumulative-partial-details`, plus
missing-base/total, explicit-zero, explicit-null and absent-optional-detail variants.
Named A10 `milestone-*` goldens distinguish text, reasoning, tool name/arguments and generated
audio, whitespace, usage-only/no output, split events, several events in one chunk, and failure
before output. The Anthropic/Gemini/Responses usage streams also assert both milestones.

**Verification follow-up (2026-10-30).** Thirteen additive regressions fix both Round 6
oracle blockers, with no accepted OTHER limitation and no changes to any of the preceding
73 fixtures. `usage-split-event-failure-before-frame`, `usage-split-delimiter-failure`,
`usage-coalesced-snapshots-failure`, `usage-split-event-completes` and
`usage-failure-before-any-chunk` pin complete-frame observation, coalesced snapshot merging,
and zero delivered chunks. `milestone-responses-tool-name-before-arguments`,
`milestone-responses-empty-tool-scaffold`, `milestone-gemini-generated-audio`,
`milestone-gemini-empty-audio-no-output`, `milestone-cohere-sse-text`,
`milestone-cohere-sse-tool-name-before-arguments`, `milestone-cohere-v1-text` and
`milestone-cohere-sse-event-header-text` pin the qualifying output forms and their metadata/
empty-scaffold negatives. Usage and milestones now share one observed-chunk/frame replay.

**JSON/output follow-up (2026-10-30).** Fourteen further additive regressions close the
remaining observation and milestone gaps without changing any of the preceding 86 fixtures.
`usage-gemini-json-array-failure-before-any-chunk`, `usage-gemini-json-array-only-opening-bracket`,
`usage-gemini-json-array-partial-usage-object`, `usage-gemini-json-array-complete-member-before-close`,
`usage-gemini-json-array-split-completes` and `usage-gemini-json-array-cumulative-prefix` cover
unobserved/partial JSON, retained complete members, split completion and prefix-only snapshot
merging. `usage-responses-json-failure-before-any-chunk`, `usage-responses-json-partial-object`,
`usage-responses-json-split-completes` and `usage-responses-json-complete-object-before-empty-chunk`
pin the same boundary for single-object Responses usage, including cached/reasoning details.
`milestone-openai-completions-text` / `milestone-openai-completions-empty-text` and
`milestone-cartesia-sse-generated-audio` / `milestone-cartesia-sse-empty-audio` pin timed positive
and missing/empty-payload negatives. JSON and SSE now use the same transport partition/failure
boundary before frame extraction; both A8 and A10 consume only those observed complete frames.

Format:

```json
{
  "name": "openai-chat-basic",
  "request": {"method": "POST", "url": "https://api.openai.com/v1/chat/completions",
              "headers": {"content-type": "application/json"}, "json": {}},
  "response": {"status": 200, "headers": {"content-type": "application/json"}, "json": {}},
  "expect": {"captured": true, "name": "chat gpt-4o-mini", "status": "OK",
             "attributes": {"gen_ai.provider.name": "openai"}, "absent": ["gen_ai.request.seed"]}
}
```

- Request body: `json`, `text`, `multipart` (`{"fields": {}, "files": [{"name", "filename",
  "content_type", "size"}]}`; file bytes are `size` zero bytes) or `binary` (`{"size",
  "content_type"}`).
- Response body: `json`, `text`, `sse` (list of `data:` payloads; objects are serialized
  as compact JSON, strings are sent as-is; an optional `event` key on an object entry is sent
  as an `event:` line and removed from the data), `ndjson` (list of objects), or `binary`.
- Request `"unavailable": true` means the hook could not copy the body: the core receives no
  request body. Response `"gzip": true` means the harness gzips the whole response body and
  sets `content-encoding: gzip`, delivering the encoded bytes to the core. Response
  `"error_after_chunks": N` means the transport fails with a timeout after delivering the first
  N chunks of the body.
- `expect.attributes` lists exact values for a subset of attributes; numbers compare by
  value, arrays element-wise. `expect.present` lists attributes that must be set (any value).
  `expect.not_contains` maps an attribute to substrings that must not appear in it (when it
  is set).
  `expect.absent` lists attributes that must not be set. `expect.status_description` is the
  exact span status description. `expect.captured: false` means no span may be created.
- When the response is binary, its body is `size` bytes delivered in two chunks.
- **Round 6 arrival replay:** `response.arrival_offsets_ms` is a list of nonnegative,
  nondecreasing milliseconds since the transport request handoff (test clock zero), one
  timestamp per response chunk. By default each SSE/NDJSON item is one chunk, using the
  compact UTF-8 JSON serialization above and `\n\n` SSE / `\n` NDJSON delimiters. Optional
  `response.chunk_sizes` repartitions the concatenated uncompressed body into the listed
  byte lengths (including delimiters); the sizes must sum to the complete wire body size,
  and their number must equal the number of arrival offsets. Empty chunks do not establish
  first-chunk time. Split events take the timestamp of the chunk completing their delimiter;
  events sharing a chunk share its arrival time. JSON bodies use compact UTF-8 JSON with no
  added delimiter, one chunk by default; a streamed JSON-array member completes at its own
  closing brace, not the later array closing bracket. Apply `error_after_chunks` **after** this
  partition for **every framing**, and ignore bytes/timestamps for unobserved chunks. Harnesses replay with a controlled
  monotonic clock or inject recorded arrival timestamps; do not sleep to approximate them,
  use parse time, or strip timing assertions to make an older SDK pass. Attribute values are
  seconds, rounded to six decimals in these fixtures. These are transport observations, not
  timestamps supplied by the remote provider.

The Python implementation in `python/fi_instrumentation/auto/wire/_core.py` is the wire-format
reference for anything this document leaves open. Privacy recognition instead follows
`privacy.json` and the catalog-driven reference in `generate_privacy_fixtures.py`.

Every SDK's test suite must run every fixture through its wire layer, including `round6/`
once adopting Round 6. The standard-library A8/A10 oracle in `generate_fixtures.py` is the
normative source for the amended usage/timing cases; an older Python parser cannot override
these explicit rules. Its checks validate the contract, not SDK lifecycle, transport timing
implementation, resource preservation or attribute-container admission.

### Round 6 known limitations (2026-10-30)

- **Timing-name exclusion scope (2026-11-01).** Names ending in a timing or throughput suffix
  (`*_latency`, `*_per_second`, `time_to_first_token`, ...) are not treated as credential
  names, so a key such as `access_token_latency` or `bearer_tokens_per_second` is kept as a
  metric. Values under such keys are expected to be numbers; any real credential value that
  crossed an instrumented transport is still scrubbed by the S5 registry wherever it appears.

- **Timing privacy/projection follow-up (2026-11-01).** Token timing values must retain their
  numeric types, and A7 must reserve present timing attributes before content.
  The guarded S6 token-timing exclusion and protected A7 **TIMING** family enforce this contract;
  generated goldens pin numeric preservation and credential precedence at every name site.
  Required SDK changes in **Python, TypeScript, Go, C#, Java and Ruby**: sync both `privacy.json`
  and `privacy-fixtures.json` shipping/embedded copies, consume the new guarded exclusion in
  the shared credential predicate, and add all seven TIMING keys to each SDK's protected set
  (including deterministic overflow ordering) before admitting content. Re-run privacy and
  projection tests at default and smaller host limits; this spec change alone does not fix SDK
  runtimes. Stale SDK copies are expected until their owners re-sync; do not run `sync.py --write`
  while other workers own those directories. `LAUNCHER.md` is unchanged.

- **Unlisted first-output shapes (2026-10-31).** `time_to_first_text` recognises the deltas
  listed in the A10 table. These shapes are not yet listed, so they produce a late or missing
  first-text milestone (first-chunk is unaffected): Chat Completions `delta.tool_calls[].custom`
  (custom tools), streamed transcription `transcript.text.delta` / `transcript.text.segment`,
  Responses shell-tool `response.shell_call_command.added` / `.delta` and `shell_call` items,
  Cohere v1 `search-queries-generation`, OpenAI speech `stream_format: "sse"` audio events and
  ElevenLabs `/stream/with-timestamps` NDJSON audio. A missing milestone is an attribute
  inaccuracy, not a capture or privacy failure; add rows (and fixtures) to extend coverage.

- **Staged SDK/harness adoption (2026-10-30).** Existing nonrecursive SDK runners exercise
  only the 47 baseline files. Each SDK owner must add recursive discovery, timed-chunk replay,
  A8 extraction, A7 pre-admission projection, A6 resource preservation and A11 neutral
  activation tests before claiming parity. The 293 additive goldens and reference checks do
  not themselves implement these runtime changes. The original 2026-10-30 Round 6 amendment
  left `LAUNCHER.md` and all privacy files untouched; the 2026-11-01 follow-up above updates privacy.
- **Reference boundaries (2026-10-30).** Arrival fixtures use SSE, NDJSON and streamed JSON arrays over
  bounded, uncompressed synthetic bodies. They do not qualify binary Bedrock event-stream
  decoding, compressed event-to-chunk timestamp association, production network latency or
  tool execution/audio playback. The A10 omission/incomplete-marker rule applies if an SDK
  loses arrival association; lack of a matching fixture is not permission to substitute parse
  time. JSON observation boundaries and supported uncompressed Completions/Cartesia SSE output
  are **not** exceptions: the JSON/output follow-up fixes both blockers with generated
  regressions; no new OTHER limitation is accepted (2026-10-30).
- **Extremely small host limits (2026-10-30).** No attribute projection can fit more protected
  keys than the host permits (including a zero-slot limit). A7 specifies deterministic
  no-content fallback and out-of-band reporting rather than silently overriding host limits.

## Node manual and zero-code batching compatibility

The manual public API is preserved: explicit **or omitted** `batch: false`
on fi-core `register()` / `FITracerProvider` selects SimpleSpanProcessor. Manual
`batch: true` selects bounded BatchSpanProcessor. The zero-code auto agent
explicitly constructs BatchSpanProcessor for its owned export route and requests
batching for its owned provider. Explicit application processors keep their policy.

| Node entry | Default/selection | Reason |
| --- | --- | --- |
| Manual fi-core, omitted or false `batch` | Simple | Preserve dev's public manual contract |
| Manual fi-core, true `batch` | Batch | Explicit caller choice |
| Zero-code owned provider/export route | Batch | Explicit agent-owned bounded queue |
| Explicit application processor list | Supplied policy, including an empty list | No hidden default exporter |

R-C07 is superseded by **zero-code owned provider batches; explicit `batch: false`
honoured**. The dev Lambda wrapper accepts the auto-returned provider, ends its
invocation span before bounded flush, and never shuts down the warm provider.
Neither queuing nor successful local handoff proves remote collector acceptance.

The shared credential catalog still masks access-key identifiers and untrusted
token-named fields. Only the explicitly bound provider-root logprob paths above
use the reviewed name-only exception and required reconstruction rule; numeric token
metrics remain available. Credential values use `<redacted>`; embedding-vector hiding is
a fi-core content policy and uses its dev `__REDACTED__` marker, not a new catalog
credential replacement. Generated privacy fixture bundles must be regenerated
and every shipping copy synchronized before this candidate is qualified.
