## Unreleased

### Changed
- Credential value shapes are scanned in time linear in the text, and a token is replaced whole whatever its length.

## [1.4.2] - 2026-11-20

### Breaking
- A lone `x-api-key` or `x-secret-key` header (any case, percent-encoded names included) or gRPC metadata entry makes a send key-carrying whatever its value, for HTTP and gRPC alike. It is judged by the same destination rules as a complete pair: plaintext to a host other than literal loopback needs `FI_ALLOW_INSECURE_HTTP=1` or `allowInsecureHttp`, a Future AGI host is refused for plaintext, and a proxy is refused. A lone key header is not treated as the caller's own header for its own collector.
- A self-hosted `http://` `FI_BASE_URL` (or `endpoint`) that sent FI keys in 1.4.1 gets its FI keys refused, so no traces are exported, unless `FI_ALLOW_INSECURE_HTTP=1` or `allowInsecureHttp: true` is set. Literal loopback (`localhost`, `127.0.0.1`, `::1`) keeps working with no setting. A Future AGI host (`futureagi.com`) always needs `https://`; no setting changes that.
- Key-carrying sends use fi-core's own pinned transport: `undici` 6.29.1, a dependency of the package, with an `Agent` of its own for direct sends. An application's global dispatcher (including one a library such as `@openai/agents` installs, or one set with the `undici` package's `setGlobalDispatcher`), `globalThis.fetch` wrappers, APM wrappers of `fetch`, mocks and polyfills are not used for these sends and do not see them; the application's own requests are unaffected: loading the SDK does not change the application's own `fetch`, its global dispatcher or the undici global slot. A send that carries no key goes direct and never reads the proxy setting. TLS verification is always on: `NODE_TLS_REJECT_UNAUTHORIZED=0` and an application's `requestTls` or `connect` option do not apply to them. A `dispatcher` option passed on a key-carrying send is refused with `FI_DISPATCHER_UNKNOWN`, whatever the dispatcher is called or looks like. A plaintext request that carries keys is not sent through a proxy. A gRPC target on a Future AGI host for which a plaintext channel is asked (an `http://` scheme, insecure credentials, `OTEL_EXPORTER_OTLP_INSECURE=true`) is refused with `FI_PLAINTEXT_FI_HOST`. A target with userinfo (`https://user@host`) or with no host (`http://:4318`) is judged by the same destination rules as a complete pair.

### Added
- `undici` 6.29.1 is a dependency (pinned exactly): key-carrying sends use an `Agent` of the package's own through undici's request API, and loading the SDK does not change the application's `fetch` or its global dispatcher. A response body that is not read is dropped, and a connection that fails while a body is arriving raises no error in the application.
- `FI_HTTPS_PROXY` and the `httpsProxy` option on `createSpanExporter`, `FITracerProvider`, `register`, `HTTPSpanExporter` and the eval checks name an SDK-owned https proxy (see the proxy setup below); `FI_PROXY_UNSUPPORTED` refuses a proxy URL that is not `http://` or `https://`.

### Changed
- One check judges every send that can carry FI keys, on the final URL and the final headers or metadata, after every merge the transport performs. It covers every key-carrying `fetch` (the deprecated HTTP exporter and the custom eval checks), the OTLP HTTP exporter's send, and each gRPC call's metadata. It refuses, with a value-free error and nothing sent: an FI key header name (any case, percent-decoded) that the decision did not put in the request or that carries another value, an FI key value (`FI_API_KEY` / `FI_SECRET_KEY`, or a key value the exporter's own options carry) under any other name, a plaintext request that carries keys when the destination rules refuse it, and a plaintext request that carries keys through a proxy. A 3xx answer to a key-carrying `fetch` is `FI_REDIRECT`; redirects are not followed. A test reads the source and fails on any `fetch`, `http.request`, undici dispatcher or gRPC client outside the guarded entry points.
- The OTLP HTTP exporter sends through the same transport: the package replaces the exporter's own send, keeping its retry layer, compression and timeout, so no global agent, proxy or dispatcher of the application is used. The deprecated `HTTPSpanExporter` and the eval checks send through it as well.
- Node's environment-proxy switch (`NODE_USE_ENV_PROXY=1`, the flag `--use-env-proxy`, or that flag in `NODE_OPTIONS`) is read once, when the package loads, as Node reads it: only the value `1` counts (`true`, `TRUE` and `yes` do not), and a value set later in the process does not turn it on. With the switch on, `HTTPS_PROXY` and `NO_PROXY` choose the SDK's https proxy (see the proxy setup), and a plaintext request that carries keys is refused with `FI_PLAINTEXT_PROXIED`. gRPC channels that carry keys in plaintext are created with proxies off.
- A refusal logs its explanatory message once per process, shared across copies of the package, to the OpenTelemetry diagnostic logger (`diag.error`, visible only when the application enables a diagnostic logger); every later refusal logs only its code, for example `not sent (FI_PLAINTEXT_NOT_ALLOWED)`. The first refusal in the process carries the message whether or not a diagnostic logger was enabled at that moment. Setting the shared latch in advance hides a message and never changes a refusal.
- Exporting behind an https proxy: set `FI_HTTPS_PROXY` (or the `httpsProxy` option, which wins over it) to an `http://` or `https://` proxy URL, or start Node with `NODE_USE_ENV_PROXY=1` and set `HTTPS_PROXY` (`https_proxy`, as Node reads it) and `NO_PROXY`, which are read once when the package loads, with Node's NO_PROXY rule. The SDK opens its own CONNECT tunnel, for https destinations only; the destination's certificate is always verified, and so is the proxy's own for an `https://` proxy. `FI_HTTPS_PROXY` and `httpsProxy` apply whatever `NO_PROXY` says. A proxy URL with any other scheme is refused with `FI_PROXY_UNSUPPORTED`. Credentials in the proxy URL go to the proxy as `Proxy-Authorization` and travel unencrypted to an `http://` proxy. The proxy URL and its credentials are not logged, reported or exported. A send that carries no key (an exporter for your own collector with no Future AGI key anywhere in play) goes direct with fi-core's own agent and is not subject to the proxy rules. A plaintext destination is refused with `FI_PLAINTEXT_PROXIED` while a proxy is configured, loopback included. gRPC does not use `FI_HTTPS_PROXY` or `httpsProxy`: a plaintext gRPC channel is created with proxies off, and a gRPC channel with TLS reads the proxy variables as `@grpc/grpc-js` reads them.
- Code that runs before the SDK loads can still tamper with modules; that is out of scope. Subscribers to undici `diagnostics_channel` events (an APM agent that subscribes to them, for example) can see the request headers of these sends, the keys included.
- Logs at every level name the host class (Future AGI host, loopback or other host) and the error class; a URL, userinfo or query, and the message of an error that may carry them, are not logged.
- A plaintext request that carries FI keys is not sent through a proxy, loopback included: it is refused with `FI_PLAINTEXT_PROXIED` when the environment-proxy switch was on at process start, and a plaintext gRPC channel is created with proxies off. Loopback is exempt from the opt-in only.
- Caller-supplied gRPC channel credentials are used as given: TLS credentials stay TLS for a loopback or Future AGI-bound target. Insecure credentials are used only when none were supplied and the target is plaintext loopback.
- A gRPC key-named `metadata` entry given more than once is checked in every value against the ambient OTLP headers.
- `FITracerProvider` with explicit `spanProcessors` runs the key decision only for an exporter of its own, so none is made when none is built.
- Verbose logs of the custom eval checks name the endpoint's host class (Future AGI host, loopback or other host), and the other verbose exporter logs show scheme, host and path with no userinfo or query.
- An exporter for the Future AGI endpoint sends the headers the SDK sets plus the ones the caller passes explicitly. An exporter for another collector sends only the headers passed for it, and the `FI_API_KEY` / `FI_SECRET_KEY` environment values are used only for the Future AGI endpoint. For HTTP and gRPC, `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_EXPORTER_OTLP_TRACES_HEADERS` apply to other collectors only. The endpoint decides which of the two an exporter is: the Future AGI endpoint is a `futureagi.com` host, or the one `FI_BASE_URL`, `FI_TRACES_URL`, `FI_COLLECTOR_ENDPOINT`, `FI_GRPC_COLLECTOR_ENDPOINT`, `FI_GRPC_URL` or the default selects, compared by scheme, host, port and trailing slash. To use the FI keys with another endpoint, pass them as `headers` or set `FI_BASE_URL`.
- FI keys go to Future AGI hosts only over TLS, on every path that sends them: HTTP and gRPC exporters, the deprecated `HTTPSpanExporter` and the custom eval checks. An `http://` Future AGI host is refused with an error that names no value (the eval checks are skipped with one log line, and `register` goes on to build its exporter, which is refused). A plaintext gRPC channel asked for a Future AGI host (an `http://` scheme, insecure `credentials` or `OTEL_EXPORTER_OTLP_INSECURE` / `OTEL_EXPORTER_OTLP_TRACES_INSECURE` set to `true` for it) is refused with `FI_PLAINTEXT_FI_HOST`. One trailing dot after a Future AGI host name is part of the same host in every decision.
- Loopback is decided on the host as written in the URL or target: only `localhost`, `127.0.0.1` and `::1` / `[::1]` keep a plaintext channel for a local collector. A spelling that a URL parser rewrites to loopback (`127.1`, `0x7f.0.0.1`, `2130706433`, `127.0.0.1.`, `localhost.`) is another host. For any other host, FI keys sent over a plaintext channel (an `http://` URL, or a gRPC target that asks for no TLS through its scheme, `credentials` or `OTEL_EXPORTER_OTLP_INSECURE`) are refused with an error that names `FI_ALLOW_INSECURE_HTTP`. A self-hosted collector without TLS is used by setting `FI_ALLOW_INSECURE_HTTP=1` (exactly `1`) or passing `allowInsecureHttp: true` to `register`, `FITracerProvider`, `createSpanExporter` or `@traceai/auto`'s `init`; one warning per process names the setting and the collector's `scheme://host:port`. The setting does not apply to a Future AGI host, or to a request that would go through a proxy. A plaintext request the SDK sends under the setting is built with proxies off, so no proxy environment variable routes it: the HTTP exporter, the deprecated exporter and the eval checks send through fi-core's own transport with a direct agent (a `dispatcher` option passed on the send is refused, and a plaintext send is never tunnelled through a proxy), and a gRPC exporter's channel is created with `grpc.enable_http_proxy: 0`.
- One decision covers every path that can carry FI keys: whether the destination is a Future AGI one, whether TLS is used and whether the keys may be attached, from the target as written and the host the transport will dial. The two readings must agree: a target that carries a backslash or a percent-encoded backslash in its authority, or an `@` after it (`grpc://grpc.futureagi.com\@other.example:4317`, `http://collector.example/x@api.futureagi.com`), or whose written host is a Future AGI host while the dialled one is not, is refused with an error that names no value, and no key is sent, whenever FI keys would be attached. A backslash ends the authority only for `http`, `https`, `ws`, `wss`, `ftp` and `file` targets. An empty authority (`https:///host/`, `https://\\host/`) is refused and is not re-read from the path. Refusals carry a `code` (`FI_HOST_MISMATCH`, `FI_EMPTY_AUTHORITY`, `FI_PLAINTEXT_FI_HOST`, `FI_PLAINTEXT_NOT_ALLOWED`, `FI_PLAINTEXT_PROXIED`, `FI_AMBIENT_KEY_HEADERS`, `FI_REDIRECT`, `FI_DISPATCHER_UNKNOWN`).
- The plaintext rule is applied to what is sent: a gRPC exporter's keys are judged on the caller's `metadata` merged with the `headers` option, so a pair in `metadata` (or split across `metadata` and `headers`) is refused over plaintext whether or not `headers` is also given. A key header given twice in different cases is judged by its non-empty entry. `allowInsecureHttp` is passed through `register` and `FITracerProvider` on every transport.
- A gRPC target named by `FI_GRPC_COLLECTOR_ENDPOINT` or `FI_GRPC_URL` keeps an `http://` prefix: it is a plaintext request and the same rule applies to it as to any other target (the prefix is kept for the decision).
- A gRPC exporter for another collector over TLS (a bare `host:port`, an `https://` URL) is unchanged and uses the `credentials` you pass. A self-hosted Future AGI gRPC collector without TLS needs `FI_ALLOW_INSECURE_HTTP=1`; with TLS it needs nothing, or use the HTTP transport.
- A request that carries FI keys does not follow a redirect: a 3xx answer is reported as an error with a message that names no value, and nothing further is sent. This covers the deprecated `HTTPSpanExporter` and the custom eval checks.
- An exporter for another collector is not created when `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` carries an `x-api-key` or `x-secret-key` header (in any case, alone or beside the other half) or a header value that contains a Future AGI key value (`FI_API_KEY` / `FI_SECRET_KEY`, or the key values of the exporter's own `headers` or `metadata`; as written or percent-encoded); the error names the two variables and no value. Another vendor's values in those variables are that collector's own and are not refused, and the check does nothing when neither variable is set. The variables are read when the exporter is constructed, and changing them afterwards is not supported.
- Constructing a Future AGI exporter throws `Unsupported OTLP exporter layout: ...` when the installed `@opentelemetry/exporter-trace-otlp-http` or `-grpc` package does not have the header configuration this release was built against. The message names no header and no value, and nothing is sent. The check works whichever copy of `@opentelemetry/otlp-exporter-base` the exporter package was built from. The accepted exporter versions are `~0.202.0` (`>=0.202.0 <0.203.0`, tested with 0.202.0); `@opentelemetry/otlp-exporter-base`, `@opentelemetry/otlp-grpc-exporter-base` and `@opentelemetry/otlp-transformer` are direct dependencies at the same range.
- An exporter's target is the one its options name, `endpoint` or the standard OTLP `url`. Giving both is accepted only when they name the same host and port, and otherwise throws without naming either. Whether a target is a Future AGI host is decided on the host the exporter dials, read the way the exporter reads it; only `http` and `https` URLs count for HTTP.
- The gRPC exporter takes caller `metadata` as headers of lower precedence than the `headers` option. With `FI_API_KEY` / `FI_SECRET_KEY` set and no `headers`, a Future AGI exporter sends the environment keys together with the `metadata`; a complete `x-api-key` / `x-secret-key` pair in `metadata` is used as given; a `headers` entry replaces a `metadata` entry of the same name. Repeated and binary (`-bin`) `metadata` values are sent for a Future AGI exporter, and every value of a credential entry is a known secret for the export privacy filter.
- The custom eval checks (`register` with `evalTags`, `checkCustomEvalConfigExists`, `checkCustomEvalTemplateExists`) send the FI keys only to the Future AGI base, under the same TLS and plaintext rules: a `futureagi.com` host or the host `FI_BASE_URL` selects. Given any other endpoint they check the base the FI environment selects.
- The Future AGI route is not a package export. `@traceai/auto` receives the Future AGI route from this package through a process-wide symbol. The factory accepts only a complete, non-empty explicit `x-api-key` / `x-secret-key` pair, reads the `headers` object once, and does not read the keys from the environment.
- `resolveTraceEndpoint(endpoint?)` returns the collector URL an HTTP exporter given `endpoint` dials, without userinfo; its log lines name no userinfo and no query.
- `register({ endpoint })` for your own collector sends no authentication unless you pass `headers`. To use the FI keys with a self-hosted deployment, set `FI_BASE_URL` to its base URL.

## [1.4.1] - 2026-11-19

### Changed
- Raw input and raw output are not recorded while any hide setting is on.
- Under `hideInputs` / `hideOutputs`, input, output and either-direction content keys, including `tool.parameters` and `tool.description`, are redacted with `__REDACTED__` on the exact key and on any dotted descendant (`key.<x>`) (#736). `gen_ai.tool.call` is redacted under `hideInputs` as well as `hideOutputs`. Nothing changes when no hide setting is on.

## [1.4.0] - 2026-11-18

### Added
- `FI_TRACES_URL` sends HTTP span exports to a trace collector separate from the platform API in `FI_BASE_URL` (self-hosted Docker installs serve them on different ports) (#710). It is a base URL like `FI_BASE_URL`; when unset or blank, exports keep their current destination. An explicit exporter `endpoint` still wins, and platform API calls keep using `FI_BASE_URL`.
- Exports keep a valid `fi.sampling.ratio` sampling weight, a number strictly between 0 and 1 (#713). It keeps a slot under attribute-count limits, down to a one-attribute budget, and is kept, instead of being replaced by the host-truncation marker, when the host's span limits dropped other attributes. A value of any other type or range is still treated as content, and a registered secret is still redacted.

## [1.3.0] - 2026-11-17

### Added
- Three supported public functions. `rememberFITracerSpan(span)` marks a span as traceAI's own, so an integration that exports converted copies of its framework's spans keeps their session, user and session-name ids. `runInFIContext(ctx, fn)` runs work with `ctx` active for traceAI, and `activeFIContext()` returns the context traceAI treats as active. Both work in an app with no OpenTelemetry context manager and register nothing with OpenTelemetry.

### Changed
- A privacy switch with an unrecognised value now hides: `FI_HIDE_*` and `FI_PII_REDACTION` accept `true`, `1`, `yes`, `on` and `false`, `0`, `no`, `off`, and any other non-empty value turns the switch on with one warning that never repeats the value. Before, only `true` turned a switch on, so for existing users this hides more, with one exception: `FI_HIDE_ERROR_DETAILS` set to `off`, `no` or `0` now shows error details, as `false` already did, where those values used to be ignored.

### Fixed
- Session, user and session-name ids are exported as set only on spans that traceAI's own tracers make, or that an integration marks with `rememberFITracerSpan`. On any other span that reaches the export boundary, such as an application span, they keep the credential name rule and are exported as `<redacted>`. A session id seen first on such a span is no longer learned as a secret, so later traceAI spans with the same id keep it.
- The active span is carried without a global context manager. In an app with no OpenTelemetry context manager, spans started with `startActiveSpan` now nest, and their span is active only inside the callback and the work it starts, never in the code that runs after it or beside it. Apps with a context manager see no change.

## [1.2.4] - 2026-11-12

### Fixed
- A full collector URL set in the environment is used as given, so traces are delivered instead of failing with 404.

## [1.2.3] - 2026-11-08
### Fix
- **Upgrade.** 1.2.0, 1.2.1 and 1.2.2 could export short base64 media payloads that carry no recognized file signature: type- or modality-declared video (`type: video`, `type: input_video`, `modality: video`) at any cap, including the default 0; type-declared images (for example `{"type":"image","data":"<base64>"}`) when a positive `base64ImageMaxLength` (or `FI_BASE64_IMAGE_MAX_LENGTH`) was set; and images whose declaration is a separate attribute from the payload (for example `parts.0.type` = `image` beside `parts.0.data`), at any cap. 1.2.3 omits all of these at any cap. Setting the cap to 0 is not a workaround.
- Covered declarations: image and video by type or modality declaration, audio by type declaration; by MIME declaration, `image/*`, `video/*`, `audio/*`, `application/pdf` and `application/octet-stream`. Payloads are recognized in the catalog payload fields (for example `data`, `bytes`, `content`, `base64`, `b64_json`), and image declarations also cover image URL fields such as `url` and `image_url`. Under video and other generic media declarations, raw base64 under other keys such as `video`, `video_url` or `url` is not covered by this declaration rule (see the known limitations).
- Carry image declarations (`type: image`, `image_url`, `input_image`, `modality: image`, and image MIME siblings) and video declarations (`type: video`, `type: input_video`, `modality: video`) into payload recognition on FI admission, standalone span creation/setters and detached export. Short declared images without a catalog signature, including a valid 1x1 BMP, are omitted even below an explicit positive cap. Typed video uses the existing generic media payload fields at every cap, just like MIME-declared `video/mp4`, `video/webm` and `application/pdf`; cap 0 alone does not repair the earlier typed-video gap. These changes close the type-only/modality recognition gaps; credential/secret masking, audio omission and image cap-marker precedence are unchanged.
- Keep image URL payload fields subject to declared base64 omission without treating ordinary HTTP image URLs as payload bytes. Preserve image hints across separate FI-authored writes.

### Known limitations
- Under a type- or modality-declared video, raw base64 stored under keys outside the catalog payload fields (for example `video`, `video_url` or `url`) is not omitted by the declaration rule; MIME-declared and signature-recognized payloads are unaffected.
- Image data nested inside a JSON-encoded string field (for example an image url inside a JSON string) may still be exported at any image cap, including the default. A structural fix is planned for a later release. Upgrade anyway: 1.2.0, 1.2.1 and 1.2.2 export this shape and more (typed video at every cap, flattened image parts at cap 0).

### Documentation
- Scope the media-omission contract to recognized base64 image payloads in payload fields or `data:` URLs, not every possible image encoding. See the [README image recognition limits](README.md#image-recognition-limits) and 1.2.0 known limitations below for non-payload keys, headerless runs and spacing.
- Document cap-0 ASCII whitespace compaction separately within each Q1 scope/view, including inside `data:` / `image/` fragments: U+0009-U+000D and U+001C-U+0020. Python also removes non-ASCII `str.isspace()` characters; TypeScript does not compact them in this check. JSON sibling tokens are never joined.

## [1.2.2] - 2026-11-08
### Fix
- Preserve numeric `gen_ai.usage.cache_read_tokens.audio` counts through admission, projection and export. The 1.2.0 shared privacy runtime mistook this modality count for an audio payload. Only nonnegative safe integers receive the metric role; strings, arrays, media ancestry and registered secrets retain their existing privacy protections.

## [1.2.1] - 2026-11-08
### Fix
- **Spans no longer carry the process command line or the OS user.** 1.2.0 added OpenTelemetry's full process detector to every provider resource, so `process.command_args`, `process.command`, `process.owner`, `process.executable.path` and `process.pid` were exported with every span. Command-line flags can hold secrets. By default fi-core now detects only `process.runtime.name`, `process.runtime.version` and `process.runtime.description`, as it detected nothing before 1.2.0.
- To opt in, set the OpenTelemetry Node variable `OTEL_NODE_RESOURCE_DETECTORS` to a comma-separated list of `host`, `os` and `process`, or `all` / `none`. `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` are always applied.

## [1.2.0] - 2026-11-07
### Changed
- 1.2.0 exports the typed session.id set with setSession; other session-named attributes remain masked.
- With hideEmbeddingVectors, the legacy embedding.model_name attribute is also hidden.
- **Recognized base64 image payloads are omitted.** The FI media policy omits recognized base64 image payloads in payload fields or `data:` URLs at any cap, with markers such as `<omitted: N base64 chars>`. The previous default cap of 32000 could retain below-cap images. This is bounded recognition, not universal image detection: see known limitations below and the 1.2.3 typed-image fix above.
- Default cap 0 redacts any value or token that mentions a data: image URL, including mentions with no payload.
- The replacement is `[image-redacted]`, for the whole value or JSON string token.
- `base64ImageMaxLength` and `FI_BASE64_IMAGE_MAX_LENGTH` keep working, with one shared default constant, `DEFAULT_BASE64_IMAGE_MAX_LENGTH` (0). With a positive value, recognized image payloads are still omitted by the media policy; at/below-cap payloads use media markers instead of the cap-0 whole-token rule. Recognition limits below still apply.
- An invalid cap resolves to 0 and logs `Invalid base64 image cap; using 0.`
- For recognized base64 image payloads in payload fields or `data:` URLs, TypeScript media omission remains stricter than Python's positive-cap behavior. See the [README image recognition limits](README.md#image-recognition-limits) for non-payload keys, headerless runs and spacing.

### Feature
- The shared export-privacy runtime and `registerSecret(value)` that the new zero-code agent `@traceai/auto` uses. See the README.

### Fix
- Non-HTTP(S) URLs in text, such as `wss://`, `redis://` or `s3://`, are no longer mistaken for base64 audio and partly omitted. Media payload and credential masking are unchanged.
- A manual `batch: false`, set explicitly or by default, keeps a simple span processor. Only the zero-code agent batches, on its own export route.
- Safe URL query parameters are kept byte-exact, and credential values in URLs are masked by the shared sanitizer. Masking of assignments in prose and headers is unchanged.
- Hidden embedding-vector fallbacks use the `__REDACTED__` marker consistently.
- Native SDK count-limit evidence is kept for late library projection, and Genkit native loss evidence is kept before mapping rewrites.

### Known limitations
- embedding.model_name may be omitted from FI exports of very large embedding batches; use gen_ai.request.model.
- The image cap does not detect headerless base64 images (PNG, JPEG, GIF, WebP without a data: URL) unless they are the whole value or a whole JSON string token, nor a declared image payload whose base64 contains spaces; the media policy may still omit them.
- An over-cap image payload stored under a key outside the payload fields (data, bytes, content, image, image_url, url, base64, b64_json) of an image-declaring object, without a data: URL, is not detected by the image cap (e.g. {"type":"image","mime_type":"image/heic","blob":"<base64>"}); other media policy may still withhold it.

## [1.1.1] - 2026-11-05
### Fix
- Export the shared PII policy for native exporters. `@traceai/mastra` requires at least this version; publish fi-core before publishing Mastra.

## [1.1.0] - 2026-10-18
### Security fix: content hiding skipped the attributes passed when a span is created
- **This bug predates 1.1.0: it is in every published release, 0.1.0 to 1.0.0.** `FITracer.startSpan` handed `options.attributes` straight to the OpenTelemetry span, so attributes passed at span creation skipped `FISpan` and every masking rule: `hideInputs` and the other hide flags, PII redaction, and (new in this release) error-detail hiding. About 22 instrumentations put the prompt there, `@traceai/openai` included, so with `FI_HIDE_INPUTS=true` the prompt was still exported in `input.value`, `gen_ai.input.messages` and `raw.input`.
- `FITracer.startSpan` and `startActiveSpan` now run creation-time attributes through the same masking as `setAttribute(s)` (`maskAttributes`). The masked attributes are still passed at creation, so a sampler sees the same values that are exported. An `error.type` passed at creation is kept.
- Content hiding now also covers the raw bodies and template variables: `hideInputs` redacts `raw.input` and `llm.prompt_template.variables`, `hideOutputs` redacts `raw.output` (as `input.value` / `output.value`).
- To get the fix, an instrumentation package must resolve `@traceai/fi-core` 1.1.0. Packages published with `^1.0.0` get it on upgrade. `@traceai/openai`, `@traceai/anthropic`, `@traceai/bedrock` and `@traceai/langchain` were published with `^0.1.x` and need the new releases that depend on `^1.1.0`.

### Feature
- New `hideErrorDetails` trace config option (env `FI_HIDE_ERROR_DETAILS`). When on, an ERROR status keeps no description, and `exception.message`, `exception.stacktrace`, `error.message` and instrumentor keys ending in `.exception.message` / `.exception.stacktrace` / `.error.message` / `.error_message` are dropped from span attributes, exception events and other events. It is a denylist, as in the Python core: `exception.type`, `exception.escaped`, `error.type` and every other attribute are kept. `FISpan` enforces it for `recordException`, `setStatus`, `addEvent` and `setAttribute(s)`, and `FITracer` for creation-time attributes, so every instrumentation that goes through `FITracer` gets it.
- Tri-state: when neither the option nor the env var is set, `hideErrorDetails` is on whenever any of `hideInputs`, `hideOutputs`, `hideInputMessages`, `hideOutputMessages`, `hideInputText` or `hideOutputText` is on. An explicit `true` / `false` (option or env) wins.
- Spans that end in ERROR now carry `error.type`: the provider error code when the error has one (OpenAI-compatible `code` such as `model_not_found`, Anthropic's body `error.type` such as `not_found_error`, Node's `ECONNREFUSED`, OpenAI `type` when there is no code), otherwise the error class name, otherwise `_OTHER`. An `error.type` set by the instrumentation is kept.
- `exception.type` is the same in both modes: a string `code` that is an identifier (as the OpenTelemetry SDK records it), otherwise the error name / class. A numeric code such as an HTTP `400` is no longer recorded as the type (the SDK alone would record `"400"`); Python records the class too.
- Exports `generateTraceConfig`, `getErrorType`, `getExceptionType`, `getErrorMessage`, `stripStackTrace` and the error / exception attribute key constants.

### Fix
- An ERROR status description is the error message only: `FISpan.setStatus` strips JavaScript stack frames and relayed Python tracebacks, and reduces `"<Name>: <message>"` (`String(error)`) to the message of the recorded exception. A multi-line message is kept whole when the description is a full `err.stack`, and a relayed Python traceback keeps its final `Type: message` line.
- `FITracer.startActiveSpan` makes the `FISpan` the active span, so `trace.getActiveSpan()` inside the callback goes through masking and the error-detail policy instead of reaching the raw span.
- `FI_HIDE_*`, `FI_BASE64_IMAGE_MAX_LENGTH` and `FI_PII_REDACTION` env vars are now read when an instrumentation is created without a `traceConfig`. Before, they only applied when a `traceConfig` object was passed.

### Behaviour change
- Attributes passed at span creation are now masked (see the security fix). If you set a hide flag, values you saw before in `input.value`, `gen_ai.input.messages`, `raw.input`, `raw.output` or `llm.prompt_template.variables` are now redacted or dropped.
- If you set any of the content-hiding flags above (option or env), error messages, stack traces and status descriptions are now hidden as well. Set `hideErrorDetails: false` (or `FI_HIDE_ERROR_DETAILS=false`) to keep them.
- If you set `FI_HIDE_*` env vars without passing a `traceConfig`, they now take effect. So do `FI_PII_REDACTION` and `FI_BASE64_IMAGE_MAX_LENGTH`.
- Error spans get a new `error.type` attribute.
- Inside `FITracer.startActiveSpan`, `trace.getActiveSpan()` returns the `FISpan`, not the SDK span. Code that casts it to the SDK `Span` or `ReadableSpan` (e.g. to read `.attributes` or `.name`) no longer works there; use the span passed to the callback through the `Span` API instead.
- `FISpan.recordException` builds the exception event itself (same keys as the SDK) instead of calling the SDK's `recordException`.

## [0.1.10] - 2025-06-10
### Feature 
- Updated OpenTelemetry to v2.0.1
- Updated dependencies
- Bug Fixes

## [0.1.9] - 2025-05-26
- Bug Fixes

## [0.1.8] - 2025-05-26
- Bug Fixes

## [0.1.7] - 2025-05-23
- Bug Fixes

## [0.1.6] - 2025-05-23
- Bug Fixes

## [0.1.1] - 2025-05-20
- Dependencies Updated

## [0.1.0] - 2025-05-19
- Initial Release