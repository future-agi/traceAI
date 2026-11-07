# @traceai/fi-core

Core OpenTelemetry instrumentation library for TypeScript applications with advanced evaluation capabilities for AI systems.

## Overview

`@traceai/fi-core` provides a comprehensive tracing solution built on OpenTelemetry that's specifically designed for AI applications. It offers custom span exporters, evaluation tags, and seamless integration with the TraceAI platform for observability and performance monitoring.

## Features

- **OpenTelemetry Integration**: Built on top of OpenTelemetry APIs with custom implementations
- **Custom Span Exporter**: HTTP-based span exporter with configurable endpoints
- **AI Evaluation Tags**: Comprehensive evaluation system for AI applications with 50+ built-in evaluators
- **Project Management**: Support for project versioning, sessions, and metadata
- **Flexible Configuration**: Environment variable and programmatic configuration support
- **TypeScript Support**: Full TypeScript support with comprehensive type definitions

## Installation

```bash
npm install @traceai/fi-core
# or
pnpm add @traceai/fi-core
# or
yarn add @traceai/fi-core
```

## Module System Support

This package supports both **CommonJS** and **ESM** module systems for maximum compatibility.

### ESM (ES Modules)
```typescript
import { register, ProjectType, EvalTag } from '@traceai/fi-core';
```

### CommonJS
```typescript
const { register, ProjectType, EvalTag } = require('@traceai/fi-core');
```

### TypeScript Configuration

For optimal compatibility, ensure your `tsconfig.json` includes:

```json
{
  "compilerOptions": {
    "moduleResolution": "node",
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true
  }
}
```

The `module` setting can be `"commonjs"`, `"esnext"`, or any other module system your project requires.

## Quick Start

### Basic Setup

```typescript
import { register, ProjectType } from '@traceai/fi-core';

// Initialize tracing with minimal configuration
const tracerProvider = register({
  projectName: 'my-ai-project',
  projectType: ProjectType.EXPERIMENT,
});
```

### Async spans and shutdown

`register()` installs the global tracer provider and a Node.js async context manager when you do not already have one. Parent spans, session context and tracing suppression then survive `await`. An existing application context manager is reused.

```typescript
import { register, ProjectType } from '@traceai/fi-core';

const provider = register({
  projectName: 'support-assistant',
  projectType: ProjectType.OBSERVE,
  batch: true,
});
const tracer = provider.getTracer('support-assistant');

await tracer.startActiveSpan('request', async parent => {
  try {
    await Promise.resolve();
    tracer.startSpan('prepare-response').end();
  } finally {
    parent.end();
  }
});

await provider.forceFlush(); // Deliver queued spans at a serverless invocation boundary.
await provider.shutdown(); // Call once when the application exits, not after each request.
```

For manual `register()` and `FITracerProvider`, omitted `batch` and explicit `batch: false` select the standard OpenTelemetry `SimpleSpanProcessor`, preserving the dev default. Set `batch: true` for bounded `BatchSpanProcessor` queuing. The zero-code auto agent explicitly selects batching for its own export route; application-supplied processors retain their own policy. Both HTTP and gRPC support either policy. Shutdown drains pending spans; it does not disable the global context manager that other providers may still use.

### Configure your own processors

Pass `spanProcessors` to `register()` or `new FITracerProvider()` when you want to own exporter configuration. This list **replaces** the default Future AGI exporter; an empty list disables export. Configure every destination you need in the list. Do not attach the same exporter twice.

```typescript
import { register, ProjectType } from '@traceai/fi-core';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';

const provider = register({
  projectName: 'support-assistant',
  projectType: ProjectType.OBSERVE,
  spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({
    url: 'https://api.futureagi.com/tracer/v1/traces',
    headers: {
      'x-api-key': process.env.FI_API_KEY!,
      'x-secret-key': process.env.FI_SECRET_KEY!,
    },
  }))],
});
```

Install the OpenTelemetry packages you import directly in your application. With explicit `spanProcessors`, `batch`, `endpoint`, `headers` and `transport` do not configure those processors; configure each exporter and processor yourself.

The existing `provider.addSpanProcessor(processor)` entry point remains supported. Its first call replaces the implicit Future AGI processor, and later calls append. If you supplied `spanProcessors` at construction, calls only append. Already-queued spans in a replaced processor are still flushed and shut down with the provider. Prefer constructor configuration before creating spans.

### Use an existing OpenTelemetry SDK

Keep one global provider. If your application already owns a `NodeSDK` or `NodeTracerProvider`, configure its processors and exporters there instead of calling `register()` again. Wrap its tracer with `FITracer` if you need TraceAI's span privacy helpers. Do not add another instrumentation layer for a framework that already emits the spans you need.

`register()` never replaces an existing global provider. It warns and returns a separate provider; global `trace.getTracer()` calls still use the application's original provider. Use `setGlobalTracerProvider: false` to intentionally create an isolated provider, obtain tracers from that returned provider and retain your application's context manager. This option does not install or change global context or propagation. Constructing `FITracerProvider` directly also leaves globals untouched.

### AWS Lambda invocation lifecycle

Wrap your exported handler with `traceLambdaHandler(handler, { provider })`. Create the provider once outside the handler, with a `BatchSpanProcessor` and a finite exporter timeout. Reuse an existing application-owned provider rather than registering a second SDK. The wrapper never installs a provider, changes global propagation or calls `shutdown()`.

```typescript
import { traceLambdaHandler } from '@traceai/fi-core';

// provider is your application's configured, reusable OpenTelemetry provider.
export const handler = traceLambdaHandler(async (event, context) => {
  return { statusCode: 200, body: 'Order accepted' };
}, { provider, maxFlushWaitMillis: 2000, remainingTimeReserveMillis: 250 });
```

The application must install an async context manager; `register()` does so when it successfully registers the global provider. Model and tool instrumentors using that provider then attach their spans to `aws.lambda.invoke`. The invocation is a SERVER span classified as `CHAIN`, with request ID, function name and a first-invocation cold-start flag.

The wrapper ends the invocation span before attempting `forceFlush()`. It waits at most the smaller of `maxFlushWaitMillis` (default 2000) and the remaining invocation time minus `remainingTimeReserveMillis` (default 250). Exhausted or invalid budgets skip flushing. A timed-out wait does **not** cancel export. One pending flush is shared across wrappers using the same provider; additional invocations do not start more flushes until it settles. New spans may remain queued for a later flush. Hard timeouts, process termination and synchronously blocking processors/providers cannot be made safe by an asynchronous timer.

Only W3C `traceparent` is extracted, from API Gateway v1/v2, function URL and ALB HTTP events. Names are case-insensitive and multi-value headers must identify a single parent. Conflicting or malformed parents, queue batches and direct invocations start independent traces. Baggage, tracestate and ambient X-Ray parents are not imported. Queue invocations have an invocation span, not per-record links or propagation.

Invocation data is metadata-only: the wrapper does not copy event bodies, result content, URLs, query strings, arbitrary headers or exception messages/stacks. For recognized HTTP proxy events it reads only the response's integer `statusCode` (100–599), records `http.response.status_code`, and marks 5xx as ERROR with the code as `error.type`; 4xx does not mark a SERVER span as failed. Thrown errors record only built-in error categories. Root error details remain hidden even with `FI_HIDE_ERROR_DETAILS=false`; there is no content-capture opt-in. Child instrumentors and application-added attributes have their own privacy policies. `provider: undefined` disables invocation tracing; validate both FI credentials and project configuration before constructing an exporter so partial authentication is never sent. Disabled, failed-initialization and incoming OTel-suppressed invocations preserve the caller's active context and do not flush.

Use this wrapper **instead of** upstream `AwsLambdaInstrumentation`, an auto-instrumenting ADOT layer or another invocation wrapper. Wrapping the same returned handler again is a no-op, including across CommonJS and ESM imports; both imports share pending-flush state. Response-streaming handlers are not instrumented and retain their original function identity/stream markers. Promise handlers work on Node 22/24/26, including async functions annotated with AWS types such as `APIGatewayProxyHandler`. The default completion mode observes the returned value or promise, not the callback; select `completion: 'callback'` for a callback implementation even if it has the same AWS type.

For Node 22 callback handlers, you must set `context.callbackWaitsForEmptyEventLoop = false` **before calling the traced handler** and keep it false until completion. This lets Lambda return without waiting for pending exporter sockets after the flush budget expires. It also stops Lambda waiting for unrelated background work, so finish all required application work before calling back. The wrapper never changes this application-owned flag. If it is true or unset on entry, the wrapper calls the original handler without invocation tracing or flushing.

```typescript
import type { APIGatewayProxyHandler } from 'aws-lambda';

const callbackHandler: APIGatewayProxyHandler = (event, context, callback) => {
  callback(null, { statusCode: 200, body: 'Order accepted' });
};
const tracedHandler = traceLambdaHandler(callbackHandler, { provider, completion: 'callback' });

export const handler: APIGatewayProxyHandler = (event, context, callback) => {
  context.callbackWaitsForEmptyEventLoop = false;
  tracedHandler(event, context, callback);
};
```

Use one completion style per handler. With callback tracing enabled, the wrapper takes the first callback result or synchronous error, ends the span, attempts flushing, then calls the runtime callback once. Use promise handlers on Node 24 and later. Do not use legacy `context.done`, `context.succeed` or `context.fail` completion methods. Enable the OpenTelemetry diagnostic logger at DEBUG to see static telemetry-failure messages; the wrapper never logs caught exception contents.

### Advanced Configuration

```typescript
import { register, ProjectType, EvalTag, EvalTagType, EvalName, EvalSpanKind, ModelChoices } from '@traceai/fi-core';

// Create evaluation tags for AI model monitoring
const evalTags = [
    new EvalTag({
        type: EvalTagType.OBSERVATION_SPAN,
        value: EvalSpanKind.LLM,
        eval_name: EvalName.CONTEXT_ADHERENCE,
        custom_eval_name: 'custom_context_check',
        mapping: {
            "context": "raw.input",
            "output": "raw.output"
        },
        model: ModelChoices.TURING_SMALL
    })
]

// Register with comprehensive configuration
const tracerProvider = register({
  projectName: 'advanced-ai-project',
  projectType: ProjectType.EXPERIMENT,
  projectVersionName: 'v1.0.0',
  evalTags: evalTags,
  verbose: true,
});
```

## Core Components

### TraceAI Provider

The `FITracerProvider` extends OpenTelemetry's `BasicTracerProvider` with custom functionality:

- Custom HTTP span exporter
- Automatic resource detection and configuration  
- Built-in UUID generation for trace and span IDs
- Manual Simple span processing by default; opt-in bounded batching, with zero-code owned routes explicitly batched; supplied processors remain application-owned

### Evaluation System

The evaluation system provides comprehensive AI model assessment capabilities:

#### Built-in Evaluators

- **Content Quality**: Context adherence, completeness, groundedness
- **Safety & Moderation**: Toxicity, PII detection, content moderation
- **Technical Validation**: JSON validation, regex matching, length checks
- **AI-Specific**: Conversation coherence, prompt injection detection
- **Custom Evaluations**: API calls, custom code evaluation, agent-as-judge

#### Span Types

- `LLM`: Large Language Model operations
- `AGENT`: AI agent executions
- `TOOL`: Tool usage and function calls
- `RETRIEVER`: Information retrieval operations
- `EMBEDDING`: Vector embedding operations
- `RERANKER`: Result reranking operations

### Project Types

- `EXPERIMENT`: For experimental AI development and testing
- `OBSERVE`: For production monitoring and observability

## API Reference


### Environment Variables

- `FI_BASE_URL`: Base URL for the TraceAI collector
- `FI_API_KEY`: API key for authentication
- `FI_SECRET_KEY`: Secret key for authentication

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

At cap 0, a scope (one JSON string token, or the whole value when it is not a
complete JSON document) is replaced whole with `[image-redacted]` when its raw
text or any decoded view contains a case-insensitive `data:`, then later
`image/`, then later `,`. A declared image payload or a bare image signature
found at cap 0 also replaces its scope with `[image-redacted]`.

**Cap-0 over-redaction disclosure:**

Default cap 0 redacts any value or token that mentions a data: image URL, including mentions with no payload.

In the FI retained/export view, the media policy (see "Media policy and image cap for this release" below) omits base64 image payloads at any cap, as `<omitted: N base64 chars>`. The positive-cap rules below describe the value-level recognizer that runs first; they never cause an image to be exported. The TypeScript SDK is stricter than the Python SDK here: it never exports base64 images at any cap.

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

**Over-redaction disclosure:** decoys, text that only discusses data URIs, and
some valid `;base64,`-bearing MIME parameters may be redacted, sometimes as a
whole value. At cap 0 (the default) any value or JSON string token that mentions a
data: image URL is replaced, including mentions that carry no payload. An image
payload stored under a key outside the payload fields above and without a data:
URL is not detected by this control. Native and application data are untouched.
This is a trace privacy
and size control, not a detector for arbitrary binary encodings or a guarantee
about every exporter/provider path.

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
`LANE-CALL-NIKM4-5A-TS-IMAGE-EXPORT-I1-DOMINATES-r1` decision makes I1 media
omission dominant at every cap in the FI retained/export view. Recognized
base64/media and data URLs are omitted, including images at or below a positive
cap; base64 image payloads use markers such as `<omitted: N base64 chars>`.
At the default cap 0, the whole-value/token `[image-redacted]` rule takes
precedence for mentions of a `data:` image URL, including mentions with no
payload. Setting a positive cap does not restore base64 image export.

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
base64 image payloads at any cap. Standalone below-cap export evidence does not
certify this composed behavior. This documents the decided release behavior,
not release authorization or a change to the documented cap default.
