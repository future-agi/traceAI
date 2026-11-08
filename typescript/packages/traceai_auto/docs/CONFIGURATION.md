# Configuration reference

[Back to quick start](../README.md) · [Framework recipes](FRAMEWORKS.md) · [Capture and privacy internals](ARCHITECTURE.md)

Configure the agent before its first preload or `init()` call. The first configuration is process-wide and terminal, including after failed initialization or shutdown. Restart to change it. `init()` returns the selected tracer provider, or `undefined` if disabled/failed; it does not return a report.

## Environment and init options

Unless noted, precedence is **code option → environment → `futureagi.toml` → default**. Code options go directly into `init(options)`; privacy options go under `traceConfig`. Lists in environment variables are comma-separated, case-insensitive names from the [coverage table](../README.md#library-coverage), plus `wire` and `http`. The denylist wins.

| Environment | `init()` option | Default and behavior |
| --- | --- | --- |
| `FI_API_KEY` | `apiKey` | Unset; resolved as a pair with the secret key |
| `FI_SECRET_KEY` | `secretKey` | Unset; required with API key only for the default FI route |
| `FI_BASE_URL` | `baseUrl` | `https://api.futureagi.com`; base or full collector endpoint. Falls back to the selected project/user profile's `base_url` before the default |
| `FI_PROJECT_NAME` | `projectName` | After code/env/file: `OTEL_SERVICE_NAME` → profile `project` → working-directory basename → `DEFAULT_PROJECT_NAME` |
| `FI_PROJECT_TYPE` | `projectType` | `observe`; `experiment` is opt-in |
| `FI_INSTRUMENTATIONS` | `instrumentations` | Empty list means all eligible integrations; e.g. `openai,wire` |
| `FI_DISABLED_INSTRUMENTATIONS` | `disabledInstrumentations` | Empty list; disable named integrations even if allowlisted |
| `FI_WIRE_CAPTURE` | `wireCapture` | `true`; enables global-fetch capture and transport credential observation when `wire` is also enabled |
| `FI_WIRE_MAX_BODY_BYTES` | `wireMaxBodyBytes` | `4194304` bytes; nonnegative finite values are rounded down, invalid values use the default. A capture cap, not an application payload-size limit |
| `FI_SAMPLE_RATE` | `sampleRate` | `1`; finite values clamped to `[0,1]`, invalid values use `1`. Parent-based root sampling for agent-owned providers only |
| `FI_AUTO_DEBUG` | `debug` | `false`; masked startup JSON report |
| `FI_CONFIG_FILE` | `configFile` | `futureagi.toml`, resolved relative to the working directory |
| `FI_PROFILE` | `profile` | `default`; selects a named profile from the FI project/user files |
| `FI_MASTRA_SPAN_PRODUCER` | `mastraSpanProducer` | `exporter`; parser accepts `exporter` or `bridge`, otherwise warns and uses `exporter`. Does **not** enable Mastra zero-code support in this release |
| `OTEL_SDK_DISABLED` | — | `false`; `true` disables initialization, including when an exporter/provider is supplied |
| — | `exporter` | Unset; standard OTel `SpanExporter`, batched by the agent. No FI credentials required; takes precedence over the default FI exporter |
| — | `spanProcessor` | Unset; explicit OTel `SpanProcessor`. Takes precedence over `exporter` and the default FI route; keeps its selected processor policy, not automatically batched |
| — | `provider` | Existing global provider, otherwise an agent-created provider. Explicit provider selects the instrumentation destination without replacing the global provider |
| — | `traceConfig` | Privacy object described below |

Except for `hideErrorDetails`, environment booleans are true only when their case-insensitive string value is `true`; `1` is not true. Prefer the literal `true` / `false` forms.

### Inherited SDK settings

These are read by fi-core or the underlying OpenTelemetry SDK, not from `futureagi.toml`. The limits and batching defaults below follow the OTel 2.x SDK used by the agent; an application-owned provider/processor may configure its own values.

| Environment | Default | Scope |
| --- | --- | --- |
| `FI_PROJECT_VERSION_NAME` | `DEFAULT` | fi-core resource version name when the agent constructs a provider; not a project-version option on `init()` |
| `OTEL_SERVICE_NAME` | SDK service default | Application `service.name`; also a fallback for FI project naming |
| `OTEL_RESOURCE_ATTRIBUTES` | Unset | Comma-separated OTel resource attributes; preserved independently of FI project routing |
| `OTEL_BSP_MAX_QUEUE_SIZE` | `2048` spans | Queue bound for the agent's batch processor |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` | `512` spans | Export batch size, no larger than the queue bound |
| `OTEL_BSP_SCHEDULE_DELAY` | `5000` ms | Scheduled batch-export interval |
| `OTEL_BSP_EXPORT_TIMEOUT` | `30000` ms | SDK batch-export timeout; auto flush/shutdown still have their shorter five-second deadline |
| `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` / `OTEL_ATTRIBUTE_COUNT_LIMIT` | `128` | Native attribute admission; span-specific configuration takes precedence |
| `OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT` / `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` | Unlimited | Native string-value limit; observed loss can withhold FI content |
| `OTEL_SPAN_EVENT_COUNT_LIMIT` / `OTEL_SPAN_LINK_COUNT_LIMIT` | `128` each | Native span event/link limits |
| `OTEL_SPAN_ATTRIBUTE_PER_EVENT_COUNT_LIMIT` / `OTEL_SPAN_ATTRIBUTE_PER_LINK_COUNT_LIMIT` | `128` each | Native event/link attribute limits |

Use `FI_SAMPLE_RATE`, not `OTEL_TRACES_SAMPLER`, for the agent-owned sampler. An existing provider keeps its own sampler. Use `FI_BASE_URL` for the default HTTP export route; gRPC transport, generic OTel exporter selection and exporter headers are not `init()` options. To choose those, supply an exporter or processor configured for your destination. The auto agent explicitly disables fi-core verbose provider/exporter logs; use `FI_AUTO_DEBUG` for its report.

### Credentials and profiles

Credentials are resolved **atomically**: both halves must come from the same source. The first complete pair wins:

1. Explicit `apiKey` + `secretKey` options.
2. `FI_API_KEY` + `FI_SECRET_KEY` environment variables.
3. `api_key` + `secret_key` in the configured `futureagi.toml`.
4. Selected profile in the nearest `.fi/config.toml`, searching upward from the working directory.
5. Selected profile in `$XDG_CONFIG_HOME/fi/config.toml`, or `$HOME/.config/fi/config.toml` (OS home directory if `HOME` is unset).

An incomplete source is ignored with a value-free warning; its half is never combined with a lower-precedence half. Only credentials, `base_url` and `project` fall back to the profile files. Keep credentials in your environment or secret manager, not a committed config file.

### Config file

A credential-free `futureagi.toml` can hold ordinary app settings:

```toml
project_name = "my-ai-app"
project_type = "observe"
instrumentations = ["openai", "wire"]
wire_capture = true
wire_max_body_bytes = 4194304
sample_rate = 1
debug = false
hide_inputs = true
hide_outputs = true
hide_error_details = true
```

Keys use snake_case: `base_url`, `project_name`, `project_type`, `instrumentations`, `disabled_instrumentations`, `wire_capture`, `wire_max_body_bytes`, `sample_rate`, `debug`, `mastra_span_producer`, and the privacy keys below. File `debug` maps to `FI_AUTO_DEBUG`. `configFile` and `profile` are selected by code/environment, not by this file.

This parser supports flat strings (single- or double-quoted), booleans, numbers and single-line string arrays, not full TOML. Comments outside quoted strings are supported. Unknown/unsupported values are ignored. A section header ends flat configuration; only `[profiles.name]` sections in the FI profile files are selected by profile name. Do not put app settings under arbitrary TOML tables.

## Privacy options

All code options below are nested under `traceConfig`; file keys are snake_case. Hide controls apply to traceAI-authored values and the agent-owned export view, not an unrelated exporter's native spans.

| Environment | `traceConfig` option | Default | Effect |
| --- | --- | --- | --- |
| `FI_HIDE_INPUTS` | `hideInputs` | `false` | Hide input values and input-message families |
| `FI_HIDE_OUTPUTS` | `hideOutputs` | `false` | Hide output values and output-message families |
| `FI_HIDE_INPUT_MESSAGES` | `hideInputMessages` | `false` | Hide input message attributes |
| `FI_HIDE_OUTPUT_MESSAGES` | `hideOutputMessages` | `false` | Hide output message attributes |
| `FI_HIDE_INPUT_TEXT` | `hideInputText` | `false` | Hide input text content |
| `FI_HIDE_OUTPUT_TEXT` | `hideOutputText` | `false` | Hide output text content |
| `FI_HIDE_INPUT_IMAGES` | `hideInputImages` | `false` | Hide image input; export masking conservatively hides raw input/message aliases too |
| `FI_HIDE_EMBEDDING_VECTORS` | `hideEmbeddingVectors` | `false` | Hide vectors on library/forwarded/generic-JSON routes; catalog wire embeddings already omit vectors |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | `hideLLMInvocationParameters` | `false` | Hide invocation parameters, `gen_ai.request.parameters` and tool schemas in the export view |
| `FI_HIDE_ERROR_DETAILS` | `hideErrorDetails` | Derived | Hide error descriptions, exception messages/stacks and related content; keep error status/type |
| `FI_PII_REDACTION` | `piiRedaction` | `false` | Apply supported PII patterns; not a guarantee of complete PII removal |
| `FI_BASE64_IMAGE_MAX_LENGTH` | `base64ImageMaxLength` | `0` | Value-level image cap. Invalid/negative/fractional values fall back to `0`; positive values do not restore base64 image export |

When error hiding is unset, any input/output value, message or text hide flag enables it. Invocation-parameter hiding additionally implies error hiding at the export boundary. Image/vector hiding and PII do not imply it. Explicit `true` or `false` overrides the derived error setting; empty/invalid environment values mean derive. Secret and media scrubbing still run with explicit `false`.

Known-secret redaction is always active on traceAI-owned exports. The registry is bounded to 1024 values of at least eight characters. `registerSecret(value)` adds an exact string without coercing objects. Unknown free-text credentials, transformed secrets and credentials hidden in unparsed bodies may remain unknown; hide sensitive content instead of relying on inference.

## Bring your own OpenTelemetry destination

With no complete FI key pair, `init({ exporter })`, `init({ spanProcessor })`, `init({ provider })`, or an existing application provider activates instrumentation **without creating an FI export route**. If neither a destination nor a complete pair exists, instrumentation is disabled. With a complete pair and a provider but no explicit exporter/processor, the agent attempts to attach its FI route to that provider.

Keep one global provider. Existing OTel JS 1.x/2.x providers retain their sampler and context manager; the agent attaches its processor where supported. An incompatible provider may trigger a warning and an isolated AI provider rather than replacement of the global SDK.

For an app without an existing SDK, install the exporter you want to use:

```sh
npm install @opentelemetry/exporter-trace-otlp-http@0.202.0
```

Save as `instrument.mjs`. Set `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to your collector's **full traces endpoint** and configure that exporter's authentication separately:

```js
import { init } from "@traceai/auto";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";

init({
  projectName: "my-ai-app",
  exporter: new OTLPTraceExporter({
    url: process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
  }),
});
```

Start with your preload instead of the default one:

```sh
node --import ./instrument.mjs app.mjs
```

The agent batches and sanitizes its supplied `exporter` route. A supplied `spanProcessor` is application-owned policy: configure that processor's privacy/export behavior yourself. Agent shutdown invokes the processor/exporter it attaches, but never shuts down an application-owned provider; do not hand it a processor whose lifecycle must remain independent.

Standard `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` preserve application identity; FI project routing does not replace `service.name`. The default FI exporter uses `FI_BASE_URL`, not `OTEL_EXPORTER_OTLP_ENDPOINT`. Standard OTel attribute-count/value limits still apply (`OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` / `OTEL_ATTRIBUTE_COUNT_LIMIT`, `OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT` / `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`). Native host loss can withhold the FI content view rather than retain a partial value.

## Lifecycle API

| Export | Contract |
| --- | --- |
| `init(options?)` | Initialize once; return the same provider on later calls, or `undefined` when disabled/failed |
| `report()` | Masked configuration, `active`, integration statuses and collector state; does not probe the collector |
| `forceFlush()` | Flush completed spans with a five-second deadline; does not consume unread responses |
| `shutdown()` | Idempotent hook removal and bounded drain/shutdown; no supported reinitialization afterwards |
| `registerSecret(value)` | Register an exact sensitive string of at least eight characters; ignore other values |

## CLI

The package installs the `traceai-auto` binary. With a local npm installation, invoke it using `npx --no-install traceai-auto` so diagnostics use the package already in your app.

| Command suffix | Behavior |
| --- | --- |
| `doctor` | Initialize, send one sampled test span, flush, print report and shut down |
| `doctor --no-check` | Initialize and print report, without the test export |
| `bootstrap` | Print detected integrations and suggested `npm install` commands; no installation |
| `bootstrap --install` | Explicitly install missing instrumentors for detected targets, using npm |
| `bootstrap --install --package-manager=pnpm` | Same installation with pnpm; `yarn` is also supported |

Bootstrap never installs target application libraries. Restart after installing instrumentors. Doctor's exit status can still be zero when the collector fails or the agent is disabled: inspect `active` and `collector` in JSON. Zero sampling or an application-owned destination can legitimately leave the collector `not_tested`.

| Integration status | Meaning |
| --- | --- |
| `instrumented` | Wire/server hook enabled, or a library patch callback ran |
| `pending` | Hook registered, compatible target import not observed yet |
| `not_installed` | Target library cannot be resolved from this app |
| `missing_instrumentor` | Target found, dedicated integration absent |
| `disabled` | Disabled by config, credentials/destination, or SDK switch |
| `manual` | Explicit wrapper/processor API required |
| `failed` | Load, registration, patch or known version incompatibility; inspect the reason |

Sources: [`config.ts`](../src/config.ts), [`index.ts`](../src/index.ts), [`cli.ts`](../src/cli.ts), and [fi-core privacy defaults](../../fi-core/src/trace/trace-config/constants.ts).
