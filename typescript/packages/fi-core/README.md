- **Node's environment-proxy switch.** Node's environment-proxy switch (`NODE_USE_ENV_PROXY=1`, the flag `--use-env-proxy`, or that flag in `NODE_OPTIONS`) is read once, when the package loads, as Node reads it: only the value `1` counts (`true`, `TRUE` and `yes` do not), and a value set later in the process does not turn it on. With the switch on, `HTTPS_PROXY` and `NO_PROXY` choose the SDK's https proxy (see the proxy setup), and a plaintext request that carries keys is refused with `FI_PLAINTEXT_PROXIED`. gRPC channels that carry keys in plaintext are created with proxies off.
- **Proxy setup.** Exporting behind an https proxy: set `FI_HTTPS_PROXY` (or the `httpsProxy` option, which wins over it) to an `http://` or `https://` proxy URL, or start Node with `NODE_USE_ENV_PROXY=1` and set `HTTPS_PROXY` (`https_proxy`, as Node reads it) and `NO_PROXY`, which are read once when the package loads, with Node's NO_PROXY rule. The SDK opens its own CONNECT tunnel, for https destinations only; the destination's certificate is always verified, and so is the proxy's own for an `https://` proxy. `FI_HTTPS_PROXY` and `httpsProxy` apply whatever `NO_PROXY` says. A proxy URL with any other scheme is refused with `FI_PROXY_UNSUPPORTED`. Credentials in the proxy URL go to the proxy as `Proxy-Authorization` and travel unencrypted to an `http://` proxy. The proxy URL and its credentials are not logged, reported or exported. A send that carries no key (an exporter for your own collector with no Future AGI key anywhere in play) goes direct with fi-core's own agent and is not subject to the proxy rules. A plaintext destination is refused with `FI_PLAINTEXT_PROXIED` while a proxy is configured, loopback included. gRPC does not use `FI_HTTPS_PROXY` or `httpsProxy`: a plaintext gRPC channel is created with proxies off, and a gRPC channel with TLS reads the proxy variables as `@grpc/grpc-js` reads them.
- **Refusal messages.** A refusal logs its explanatory message once per process, shared across copies of the package, to the OpenTelemetry diagnostic logger (`diag.error`, visible only when the application enables a diagnostic logger); every later refusal logs only its code, for example `not sent (FI_PLAINTEXT_NOT_ALLOWED)`. The first refusal in the process carries the message whether or not a diagnostic logger was enabled at that moment. Setting the shared latch in advance hides a message and never changes a refusal.
- `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_EXPORTER_OTLP_TRACES_HEADERS` are read when an exporter is constructed (changing them afterwards is not supported). An exporter for another collector is not created when they carry a Future AGI key header or a Future AGI key value (`FI_API_KEY`, `FI_SECRET_KEY`, or a key value of the exporter's own `headers` or `metadata`).
- `FI_API_KEY`: API key for authentication
- `FI_SECRET_KEY`: Secret key for authentication
- `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and the other `FI_HIDE_*` switches (including `FI_HIDE_LLM_INVOCATION_PARAMETERS` and `FI_HIDE_ERROR_DETAILS`), and `FI_PII_REDACTION`: privacy switches. `true`, `1`, `yes` and `on` turn a switch on, and `false`, `0`, `no` and `off` turn it off, in any case. An empty value counts as unset. Any other value turns the switch on, so the content is hidden, and logs one warning that names the variable but not its value. The warning goes to the OpenTelemetry diagnostic logger, so it is printed only when your application has set one with `diag.setLogger`.

## Examples

### Simple LLM Tracing

```typescript
import { trace } from '@opentelemetry/api';
import { register, ProjectType } from '@traceai/fi-core';

// Initialize
register({
  projectName: 'llm-chat-bot',
  projectType: ProjectType.EXPERIMENT,
  evalTags: [
new EvalTag({
            type: EvalTagType.OBSERVATION_SPAN,
            value: EvalSpanKind.LLM,
            eval_name: EvalName.CHUNK_ATTRIBUTION,
            config: {},
            custom_eval_name: "Chunk_Attribution_5",
            mapping: {
            "context": "raw.input",
            "output": "raw.output"
            },
            model: ModelChoices.TURING_SMALL
        })
  ]
});

// Create traces
const tracer = trace.getTracer('my-app');
const span = tracer.startSpan('llm-completion');

span.setAttributes({
  'llm.model': 'gpt-4o-mini',
  'llm.prompt': 'What is the capital of France?',
  'llm.response': 'The capital of France is Paris.'
});

span.end();
```

## Development

### Building

```bash
pnpm build
```

### Testing

```bash
pnpm test
```

### Linting

```bash
pnpm lint
```

## Value-level image cap and PII

`FITracer` / `FISpan` apply `base64ImageMaxLength` (environment variable
`FI_BASE64_IMAGE_MAX_LENGTH`) independently of `piiRedaction`. Its default is
**0**, sourced from `DEFAULT_BASE64_IMAGE_MAX_LENGTH`. An explicit option wins
over the environment. Invalid options (including non-finite, negative,
fractional or non-number values) and invalid environment values resolve to
**0**, with a warning that never includes the value; invalid input never restores
a positive default or an environment cap, and there is no implicit unlimited
setting.

At cap 0, a Q1 scope (one JSON string token, or the whole value when it is not
a complete JSON document) is replaced whole with `[image-redacted]` when its
raw text or any decoded view contains a case-insensitive `data:`, then later
`image/`, then later `,`, **after ASCII whitespace compaction within that view**.
TypeScript removes exactly TAB, LF, VT, FF and CR (U+0009-U+000D), plus
U+001C-U+001F and SPACE (U+0020): the ASCII subset of Python `str.isspace()`,
not JavaScript `\s` or only WHATWG ASCII whitespace. This includes whitespace
inside the fragments: `da\tta:ima\nge/png;base64,QQ==` (with TAB and LF) triggers
whole-scope redaction. Compaction never joins JSON sibling tokens or different
Q1 scopes. Python additionally removes non-ASCII `str.isspace()` characters;
TypeScript does not compact those non-ASCII whitespace characters in this test.
Both retain their existing decoded-view/ignorable-character handling. A
declared image payload or a bare image signature found at cap 0 also replaces
its scope with `[image-redacted]`.

**Cap-0 over-redaction disclosure:**

Default cap 0 redacts any value or token that mentions a data: image URL, including mentions with no payload.

In the FI retained/export view, the media policy (see "Media policy and image cap for this release" below) omits **recognized base64 image payloads in payload fields or `data:` URLs** at any cap, using markers such as `<omitted: N base64 chars>`. The positive-cap rules below describe the value-level recognizer that runs first, not permission to export those recognized payloads. For these recognized images, TypeScript media omission remains stricter than Python's positive-cap behavior. This is not universal image detection; see [Image recognition limits](#image-recognition-limits) for non-payload keys, headerless runs and spacing.

With a positive cap, the image cap uses occurrence redaction with a whole-value fail-closed fallback
in the FI retained/export view. An occurrence counts only when its length is
**strictly greater** than `base64ImageMaxLength`; a nonempty payload is required.
Exact raw occurrences become `__REDACTED__`. A decoded-only occurrence, an
uncertified raw endpoint, or continuing normalization beyond the three-pass bound
replaces the entire owning value or JSON string-token content with
`[image-redacted]`. A whole fallback dominates narrower edits in the same scope.

The quote-independent detector looks for case-insensitive `data:`, optional
whitespace or Unicode format characters, `image/`, and then `;base64,` ending
within **1024 UTF-16 units from the prefix start**. Any characters, including
quotes and commas, may appear between `image/` and the flag. Every prefix and
every qualifying flag is inspected independently; an earlier candidate cannot
hide a later one. Payloads use the base64/base64url alphabet and padding
(`[A-Za-z0-9+/=_-]`). Complete transport escapes into that alphabet extend an
exact raw run. Existing internal CR/LF continuation is retained; trailing CR/LF
and ordinary delimiters remain outside an exact occurrence. Occurrences use the maximum of their original raw span and decoded span
lengths. Thus transported occurrences can cross the cap even when their decoded
form is at or below it. This includes original escapes in JSON string tokens.

A separate positive-only MIME recognizer retains genuine long-header and
non-final `base64` encoding-flag coverage. It cannot veto a quote-independent
match. For a transport-revealed prefix it also checks the MIME suffix as it
existed before decoding consumed a literal quoted-pair; an uncertified
occurrence selects whole B, never a quote-based exemption. Bare PNG/JPEG/GIF/WEBP base64 signatures and declared image payload fields
keep their existing greater-than threshold and, at a positive cap, the
`__REDACTED__` marker (at cap 0 they use `[image-redacted]`, see above). Payload
fields are `data`, `bytes`, `content`, `image`, `image_url`, `url`, `base64` and
`b64_json`. Declarations use `type` (`image`,
`image_url`, `input_image`), `modality: "image"`, or an `image/*` MIME value in
`media_type`, `mime_type`, `mimeType` or `mediaType`. Context reaches `source`,
`inlineData`, `inline_data`, `image_url` and `image` child objects (for the
payload fields listed first), but not through arrays. Outside the declaring
object, arbitrary alphabet-only metadata and unsigned, undeclared binary are not
image payloads.
Declared ownership follows the bounded logical-token decoding views; declared
payload thresholds remain measured on logical payload text, not transport size.

Detection examines unmodified raw text and up to three decoded views. Each pass
recognizes JSON simple and Unicode escapes, complete UTF-8 percent encodings,
HTML/XML numeric and HTML5 named character references, and removes Unicode
format characters and zero-width variation selectors. An additional equality
probe establishes whether the third view is stable; it is not a fourth searched
view. An input still changing after the third pass selects the whole fallback,
even if it otherwise looks ordinary. Decoding is for inspection: unchanged
values retain their exact original encoding. A decoded occurrence must join to
its own actual raw prefix and flag with a covered endpoint before exact raw
redaction is allowed. Merely lying inside another edit is not such a certificate.

A complete lexical JSON parse takes precedence over whole-view detection,
including when a decoded view reveals the document. Member names and string
values are inspected independently; a key-ending header fragment is never
joined with its value. Changes stay inside original token boundaries, preserving
JSON syntax, numeric lexemes, member order and duplicate lexical members. No
object is parsed and reserialized into a JavaScript map. Fixed replacement keys
can collide: lexical member count is retained, but a later consumer's
`JSON.parse` can collapse duplicate names. Unchanged documents remain
byte-identical. Nested JSON string documents receive the same fence. Malformed
JSON receives positive declaration recovery without being certified or repaired.
An over-limit damaged declared image token is redacted; a legally escaped
non-base64 control is not reclassified as damage merely because another view
reveals the control character.

### Image recognition limits

**Over-redaction disclosure:** decoys, text that only discusses data URIs, and
some valid `;base64,`-bearing MIME parameters may be redacted, sometimes as a
whole value. At cap 0 (the default) any value or JSON string token that mentions a
data: image URL is replaced, including mentions that carry no payload. An image
payload stored under a key outside the payload fields above and without a data:
URL is not detected by this control. Headerless PNG/JPEG/GIF/WebP base64
signatures are recognized by the image cap only as a whole value or whole JSON
string token, not as arbitrary embedded runs. The image cap also does not
recognize a declared payload whose base64 contains spaces. Independent media
rules may still omit these cases, but are not a universal detector: non-payload
keys such as `blob`, unrecognized headerless runs and spacing remain bounded
recognition limitations. Image data nested inside a JSON-encoded string field
(for example an image url inside a JSON string) may still be exported at any
image cap, including the default. A structural fix is planned for a later release.
Native and application data are untouched. This is a
trace privacy and size control, not a detector for arbitrary binary encodings
or a guarantee about every exporter/provider path.

The internal scan-admission limit is **1,048,576 UTF-16 units per string value**,
independent of the caller's image cap. Above it, the complete value is replaced
with `[image-redacted]`, including oversized JSON; no truncated prefix is passed
through. Admitted work retains the **32 credits per original owning-scope unit**
budget. Raw JSON outer string tokens have independent budgets; their descendants
share that budget. Repeated candidate scans are charged before work, and work
exhaustion selects the whole owning-scope fallback. Run suffixes are cached and
edits are unioned by a linear dense sweep rather than repeated payload walks or
sorting. These are source-level work bounds, not measured time, memory or
allocation guarantees.

**Counting and integration disclosure:** image-cap redactions are marked in
place; no per-span image-cap count is emitted. The helper internally counts
actual emitted replacements after union/subsumption; literal marker text adds
zero. The public `capBase64Images` return type and package exports are unchanged.
The image cap itself never adds its internal count to
`traceai.capture.dropped_content_count` and exports it under no alternate name;
where that attribute is present (including `FITracer` library spans), it counts
content keys omitted by the A7 projection, as described under "Export privacy and
application identity" below. Adding the image-cap count to it at the Node
auto-mode export boundary would be a separate change that needs a real
route-level count test; this merge does not include or claim that integration.
No new public attribute is introduced. Runtime,
performance and independent exact-byte verification remain outstanding; this
source change is not a release or security-acceptance record.

The retained-value cap runs before opt-in PII redaction. These value passes cover
creation/set span attributes, link and event attributes, recorded exception text,
ERROR status messages, and synthesized `error.type`. Explicit `error.type`
precedence is retained. Key-based hide rules and `hideErrorDetails` remain
stronger controls. Event attributes, including recorded exception events,
receive the value passes and then the same key-based content rules as span
attributes (`maskAttributes`). Calls that bypass `FITracer` / `FISpan` are
outside this wrapper's coverage. PII is disabled by default; enabling it also
processes string-array values. The cap does not make a claim that every possible
PII pattern, exporter or provider path is covered.

## Export privacy and application identity

Every traceAI-owned HTTP/gRPC exporter, including the legacy constructors, applies
the same detached S1 privacy snapshot immediately before serialization. Recognition
comes from the byte-identical shared `privacy.json` catalog. Environment credentials
and FI headers, including ignored halves of incomplete pairs, seed the process-wide
bounded registry. `traceConfig` and `FI_HIDE_*` controls also apply on manual routes.
Application-supplied processors/exporters remain application-owned and are not
silently scrubbed or replaced. Manual explicit/default `batch: false` uses Simple;
`batch: true` opts into Batch. The zero-code agent explicitly batches its owned route,
and `spanProcessors` selects an explicit SDK policy. Manual SDK owners
must await `provider.forceFlush()` or `provider.shutdown()` before explicitly
terminating the process; the auto agent manages its own bounded exit/signal drain.

When an applicable `FI_HIDE_*` / `traceConfig` policy replaces a value or JSON
member with `__REDACTED__`, that fixed marker takes precedence over catalog media
omission markers, including during recursive JSON/array scrubbing and re-export.
An already-hidden member must not become `<omitted: N base64 chars>` or disclose
length, encoding or byte-count metadata through its replacement. For example,
error-detail hiding replaces only the top-level error string in a Mastra
`processor_run` input, preserving unrelated input; output-hiding flags imply this
error policy unless `hideErrorDetails` / `FI_HIDE_ERROR_DETAILS` explicitly
overrides it. Explicit false does not disable independent credential/media
scrubbing. This precedence does not change the catalog markers for actual media
or the separately documented usage, audio-byte and capture-loss counters.

For the closed, versioned `logprob_tokens` catalog paths, **logprob tokens are checked against registered secrets and credential shapes, not key-name rules**.
This exception is available only from the parsed OpenAI chat response root at the
instrumentation handoff: `choices[*].logprobs.content[*].token` and
`choices[*].logprobs.content[*].top_logprobs[*].token`. It does not apply to nested
JSON in user content, tool output, arbitrary attributes, or flattened key names.
If value masking changes any choice's `message.content` or `delta.content`, its
**entire logprobs block** is masked, including alternatives and byte arrays.
Text already carrying an image-cap marker (`__REDACTED__` or `[image-redacted]`)
counts as changed.
Buffered streaming responses are scanned by choice index before export; once a
choice is masked, earlier buffered blocks and all later blocks for that choice
are masked too. Concatenated deltas are checked for split secrets, and affected
delta text is masked. A masked individual token also loses its byte-array alias.
The staged OpenAI instrumentor exports the buffered response once, not each delta.
Other provider structures have no new exception without catalog/source evidence.

`register({ projectName })` sets FI `project_name`, not `service.name`. Default OTel
resource identity, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` and an optional
application `resource` are preserved.

On spans made by an FI tracer (`FITracer`, which traceAI instrumentors use), and
on spans that a traceAI integration converts from its framework's spans (such as
Genkit's), `session.id` and `user.id` are exported as set, so don't use a
credential or a session cookie as either. The same applies to the session ids that
the Pi coding agent, Claude Agent SDK and Genkit integrations record under their
own keys: `pi.session.id`; `claude_agent.session.id` and `claude_agent.session_id`,
plus the resumed and forked-from session ids `claude_agent.resume_session_id`,
`claude_agent.session.previous_id` and `claude_agent.session.fork_from`; and
`genkit:metadata:agent:sessionId`. An integration marks its converted spans with
`rememberFITracerSpan(span.spanContext())`. Spans made by any other tracer, such
as application spans that `@traceai/auto` forwards, keep the credential name rule
for these keys, so a session-named value on them is exported as `<redacted>`.
Their values are still never registered as secrets, so an FI span with the same
session id keeps it.

Resource detection is minimal by default: the provider adds the Node.js runtime
name and version (`process.runtime.*`) and nothing that identifies the machine,
the OS user or the command line (which can hold secrets passed as flags). To add
OpenTelemetry's host, OS or full process attributes, set
`OTEL_NODE_RESOURCE_DETECTORS` to a comma-separated list of `host`, `os` and
`process`, or to `all`; `none` turns detection off.

`FITracer` applies the shared privacy policy before SDK attribute-value truncation.
Its library spans use the same deterministic count-limit projection as wire spans
(spec A7), including late output/usage updates. Protected attributes are reserved
first and stay plain attributes: degradation markers, identity (`gen_ai.span.kind`,
response ID, FI project routing), outcome (`error.type`, finish reasons, HTTP
status), provider/model/operation, every known `gen_ai.usage.*` counter and
provenance (`traceai.capture.layer`, `traceai.wire.endpoint`, method/server). Bounded
content then fills the remainder: output message groups, then output
value/transcript, then a contiguous newest-first suffix of input messages, then other
input content; groups (role/content/tool calls, tool definitions, a media MIME
companion) are atomic, and aggregate `input.value` is omitted once any input message
was. Remaining slots take optional metadata. `traceai.capture.dropped_content_count`
counts omitted content keys (accumulating a supplied value); the SDK's
`droppedAttributesCount` separately counts every omitted key. When even the
protected set exceeds the host limit, no content is admitted and the family-ordered
protected prefix is kept. The retired `traceai.attribute_projection.metadata`
capsule is no longer produced; the export boundary still re-sanitizes one by its
members' original keys.
Image hiding (`hideInputImages` / `FI_HIDE_INPUT_IMAGES`) does not by itself imply
error-detail hiding. Plain error text can remain when `hideErrorDetails` is false;
independent credential, media, image-cap and optional PII scrubbing still apply to
error attributes, exception events and status descriptions.

Media recognition consults the complete sibling set before A7 projection and before
`FITracer` admits attributes, events or links to the SDK. The detached export boundary
also fails closed on SDK/projection attribute loss or strings reaching the SDK value
limit: catalog media paths (including descendants below metadata boundaries) and
ambiguous base64 are omitted. A truncated MIME is never evidence of safe text. Only
unchanged values whose complete non-media MIME was observed before admission can
retain that evidence; it is held outside telemetry and never changes host spans.

## Known limitations

- **2026-11-01 (A7 small host limits):** With a host attribute-count limit at or below
  the protected set (a wire span carries about 17 protected attributes), no content
  is admitted, and provenance (`traceai.capture.layer`, `traceai.wire.endpoint`) is
  the first protected family to be cut. Earlier Node releases always reserved one
  slot for `output.value` and packed overflow metadata into a JSON capsule; both
  behaviours were retired for the cross-SDK contract. Use a practical host limit
  (the default 128) when output must be retained.

- **2026-10-31 (lossy SDK media context):** Conservative export fallback can omit
  ordinary text/base64 and metadata descendants when attributes were dropped or a
  string equals the SDK value-length limit; OTel has no truncation counter, so
  equality is intentionally treated as possible truncation. This includes ordinary
  all-alphanumeric strings of four or more characters and `content`/`value`/`data`
  paths without pre-admission text evidence. Even a MIME string such as
  `application/octet-stream` can receive a base64 omission marker: these markers
  describe conservative recognition, not proof that the original value was media.
  Protected identity/usage survives, but bounded A7 content can be over-omitted.
  Loss mode also applies inside JSON, nested JSON strings, URL queries and prose;
  malformed JSON tails are independently treated as incomplete, even if a mapper
  erased SDK limit slots. Before Genkit mapping, the auto agent records native
  at-limit/count-loss evidence as a sticky weak SpanContext bit, alongside the
  code-configured limit. Path/identity scrubbing cannot erase that bit; native
  spans/content are not retained or changed. Copies without recorded source evidence
  fall back to the global SDK's normalized limit before environment settings
  (including manual Genkit). A finite fallback limit on such a copy treats URL
  queries as potentially incomplete even for shorter values, preserving complete
  non-URL text; an unrelated global provider can over-omit those query parameters.
  Custom exporters/processors that discard both limit information and loss counters
  can still conceal loss in otherwise well-formed values. Use `FITracer` before SDK
  admission, or the `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` controls for such routes.
- **2026-10-31 (unterminated quoted prose; OTHER):** Free text with no enclosing
  JSON object, such as an `upload failed:` message followed by quoted `data` and
  `mimeType` pairs, is not a structurally framed request-body echo. If the SDK cuts
  inside the quoted value, the prose fallback requires its missing closing quote
  and may retain a short base64 prefix (even in loss mode). Complete long `data`
  values are omitted by the catalog length rule. Use `FI_HIDE_ERROR_DETAILS` for
  errors and `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` for captured input/output prose.
- **2026-10-31 (pre-existing credential prefixes; outside RC2 media scope):**
  Exact known-secret matching does not guarantee removal of an SDK-truncated
  credential prefix on native host spans, or event/link prefixes truncated before
  late transport discovery. Media omission is not a credential-prefix guarantee.
  Use pre-admission `FITracer` and the input/output/error-detail hide controls.
- **2026-10-30 (manual credential discovery):** Without the auto agent's transport
  hooks, the registry knows FI/environment credentials, structurally marked
  captured fields and explicit `registerSecret` values. It cannot infer unmarked,
  never-observed credentials or arbitrary application transformations. Use
  `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` and
  `FI_HIDE_ERROR_DETAILS` or explicitly register sensitive exact values. The shared
  LRU retains at most 1024 values; re-register credentials when reused.
- **2026-10-30 (recognition bounds):** Catalog depth/node/text exhaustion fails closed
  per span, preserving identity/timing and dropping content. Recognition is
  structural, not semantic inference; use the hide controls for stronger protection.
  Raw SDK resource arrays above 4096 pairs fail closed rather than cropping away
  MIME siblings. An `FITracer` library span exhausting its bounded admission view
  remains content-free through end. Opaque custom Span implementations without the
  SDK's writable attribute storage cannot support late attribute replacement; use
  a standard OTel SDK span or configure sufficient attribute capacity.

## Contributing

This package is part of the TraceAI project. Please refer to the main repository for contribution guidelines.

## Links

- [GitHub Repository](https://github.com/future-agi/traceAI)
- [Issues](https://github.com/future-agi/traceAI/issues)
- [TraceAI Platform](https://api.futureagi.com)

## Shared Mastra export privacy

Owned Mastra transports use this same export-time boundary, not a separate
integration filter. Input/output aliases, processor-run error input members and
model-step HTTP response bodies follow the shared hide flags, including event
attributes. Complete credential assignments in diagnostic text, Authorization
and Cookie lines, and bounded embedded JSON use the same credential predicate
as structured attributes. Access-key identifiers remain masked. Generic Mastra
attributes have no provider-root logprob exemption; the narrowly bound OpenAI
exception and reconstruction rule are described above. Catalog credential-shape
rules supplement registered-secret matching; arbitrary unmarked text and
transformed secrets remain outside exact-secret recognition.

### Node batching compatibility

| Entry | Processor policy | Lifecycle |
| --- | --- | --- |
| Manual `register()` / `FITracerProvider`, omitted `batch` or `batch: false` | Simple, unchanged from dev | Await provider flush/shutdown before termination |
| Manual `batch: true` | Bounded Batch | Await provider flush/shutdown |
| Zero-code owned provider/export route | Explicit Batch | Auto exit/signal drain; in Lambda pass the returned provider to `traceLambdaHandler` |
| Explicit `spanProcessors` / auto `spanProcessor` | Application-selected policy | Ownership remains with the documented caller/agent route |

The Lambda wrapper ends its invocation span and waits for provider flush within
its existing invocation budget; it does not shut down a warm provider. Batching
is not a collector-acceptance guarantee. R-C07 is superseded by **zero-code owned
provider batches; explicit `batch: false` honoured** to preserve the public manual
API while making zero-code ownership explicit.

### Media policy and image cap for this release

For this release, the ratified
media policy makes I1 media
omission dominant at every cap in the FI retained/export view. Recognized
base64/media and data URLs are omitted, including images at or below a positive
cap; recognized base64 image payloads in payload fields or `data:` URLs use
markers such as `<omitted: N base64 chars>`. Since 1.2.3, `type: image`,
`type: image_url`, `type: input_image`, `modality: image` and image MIME siblings
carry image context to their payload fields even for short, below-cap images
without a catalog magic signature. Ordinary HTTP image URLs remain text.
The same release also carries `type: video`, `type: input_video` and
`modality: video` into the existing generic media payload fields (such as
`data`, `bytes` and `content`) on admission, standalone span creation/setters
and detached export. These declared video payloads are omitted at every image
cap, just like MIME-declared `video/mp4`, `video/webm` and `application/pdf`;
cap 0 alone is not a workaround for the earlier typed-video gap. Video retains
the generic media metadata boundaries, not the image-only URL payload fields.
Audio omission and credential/secret masking are unchanged.
At the default cap 0, the whole-value/token `[image-redacted]` rule takes
precedence for mentions of a `data:` image URL, including mentions with no
payload. Setting a positive cap does not restore export of these recognized
base64 image payloads. See [Image recognition limits](#image-recognition-limits)
for the non-payload-key, headerless-run and spacing limitations.

`base64ImageMaxLength` and `FI_BASE64_IMAGE_MAX_LENGTH` remain supported, with one
shared default constant, `DEFAULT_BASE64_IMAGE_MAX_LENGTH` (**0**; see "Value-level
image cap and PII" above). The cap is by value, not by attribute name: oversized
image data URL occurrences, declared-image JSON field values and bare
PNG/JPEG/GIF/WEBP base64 values are replaced, never truncated. It applies
before optional PII redaction to attributes, event attributes, recorded exceptions
and status descriptions (and to link attributes). Recording an exception emits
one event. Apart from the cap-0 rule and the whole-value fail-closed fallbacks
described above (scan-admission limit, work exhaustion, decoding still changing
after three passes, uncertified or decoded-only occurrences), the cap mechanism
alone preserves at/below-limit values; the media export policy still omits
recognized base64 image payloads in payload fields or `data:` URLs at any cap,
subject to the [recognition limits](#image-recognition-limits). Standalone
below-cap export evidence does not
certify this composed behavior. This documents the decided release behavior,
not release authorization or a change to the documented cap default.
