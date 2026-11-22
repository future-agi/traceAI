## [Unreleased]

### Changed
- Credential value shapes are scanned in time linear in the text, and a token is replaced whole whatever its length.

## [1.5.2] - 2026-11-20

### Changed
- An exporter you point at your own collector is built and sends as configured when `FI_BASE_URL` or `FI_GRPC_URL` is set, including when the OpenTelemetry exporter's `__init__` is wrapped by a function that takes `*args, **kwargs`. Whether an exporter sends to Future AGI is decided from the endpoint it resolved, and its arguments are read against the exporter's own `__init__` parameters. An exporter that resolves to Future AGI with a header layout this release cannot set raises `UnsupportedOTLPExporterError`.
- Each exporter is built once, with the arguments you pass, and its Future AGI headers are set after the endpoint it resolved is known. Headers you pass are read once, as a mapping, a sequence of pairs or an iterator.
- Header names that differ only in case are one header on HTTP and gRPC: an empty duplicate does not replace a value you gave, and a header whose value is `None` is left out. On gRPC, and on HTTP to Future AGI, names are sent in lower case; on HTTP to your own collector, a name is sent as you wrote it (the last spelling given when they differ in case). Values are sent as given.
- Future AGI keys go to Future AGI hosts only over TLS: they go only to a futureagi.com host that both URL parsers read the same way, over https or TLS, or to the self-hosted endpoint named by `FI_BASE_URL`, `FI_TRACES_URL` or `FI_GRPC_URL`. A gRPC target on a futureagi.com host is reached over TLS only: the `insecure` argument, `OTEL_EXPORTER_OTLP_INSECURE=true`, `OTEL_EXPORTER_OTLP_TRACES_INSECURE=true` or an `http://` scheme on it raises `UnsupportedFutureAGIEndpointError` with the code `insecure_futureagi_grpc`. Breaking: a gRPC target on a futureagi.com host with a setting that asks for a plain channel raises `insecure_futureagi_grpc`; remove the setting (or set it to `false`), or point `FI_GRPC_URL` at your own collector. One trailing dot on a host is read as absent in every host decision.
- A plain channel (`http://`, or gRPC without TLS) that carries Future AGI keys is allowed to loopback and to nothing else by default. Loopback is decided on the host as written, and only `localhost`, `127.0.0.1` and `::1` count: `127.0.0.1.`, `127.1`, `0x7f.0.0.1`, `2130706433`, `localhost.` and `*.localhost` are self-hosted hosts like any other. Without the opt-in, constructing the exporter raises `UnsupportedFutureAGIEndpointError` with the code `insecure_self_hosted_target`, which names `FI_ALLOW_INSECURE_HTTP`. For a self-hosted collector without TLS, set `FI_ALLOW_INSECURE_HTTP=1` (the exact value `1`) or pass `allow_insecure_http=True` to the exporter, `get_custom_eval_template` or `check_custom_eval_config_exists`; one log warning per process then names the setting and the origin. The opt-in does not apply to a futureagi.com host: an `http://` URL for one raises the same error with the code `insecure_futureagi_http` (HTTP) or `insecure_futureagi_grpc` (gRPC). Breaking: a plain `http://` or non-TLS gRPC endpoint on a host other than loopback raises `insecure_self_hosted_target` unless you set `FI_ALLOW_INSECURE_HTTP=1` or pass `allow_insecure_http=True`, or use `https://` (or TLS); an `http://` URL on a futureagi.com host raises `insecure_futureagi_http`, so change it to the `https://` Future AGI URL.
- The transports this package builds for a plain channel that carries Future AGI keys read no proxy: the `requests` session of an HTTP exporter and of the eval requests has `trust_env=False` and no proxies, and a plain gRPC channel is opened with `grpc.enable_http_proxy` set to `0` (a `grpc.enable_http_proxy` or `grpc.http_proxy` channel option you pass is replaced). `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, `grpc_proxy` and the other proxy variables do not change where such a request goes. Breaking: a plain channel to a self-hosted collector that you reached through a proxy is sent directly; use TLS, or put the proxy in front of a TLS endpoint. A channel with TLS is routed as before.
- A `requests.Session` you pass to an exporter is checked when the channel is plain: the exporter raises `UnsupportedFutureAGIEndpointError` with the code `proxied` when the session names a proxy in `proxies`, when `trust_env` is on and `requests` or the standard library (`HTTP_PROXY` or `ALL_PROXY`, read with `NO_PROXY`) routes the request through a proxy, or when the session cannot be inspected: it has a hook (`hooks['response']` or any other), its class or an adapter's class has a routing function (`request`, `send`, `prepare_request`, `get_adapter`, `merge_environment_settings`, `rebuild_proxies`; for an adapter `send`, `get_connection`, `get_connection_with_tls_context`, `proxy_manager_for`, `init_poolmanager`) that is not the one `requests` had when `fi_instrumentation` was imported (a test double such as `responses` or `vcr` replaces `HTTPAdapter.send` on the class), a routing function is replaced on the session or adapter object itself, an adapter is not exactly `requests.adapters.HTTPAdapter` (a subclass is refused whatever it overrides), `hooks` is not a plain `dict`, or its `auth` is not unset, a `(user, password)` pair or an authentication handler of `requests`. A plain channel sends with an isolated copy of the session made when the exporter is built: its own adapters with the same settings and TLS configuration (an ssl context an adapter subclass holds, `verify`, `cert`), no proxies, `trust_env=False` and no hooks. What you change on your own session afterwards (proxies, adapters, hooks, the environment) does not reach what sends, and the same on OpenTelemetry 1.29 and 1.45. The copy is checked again on every export: if it was replaced or changed (a proxy, an environment proxy, a hook, a mounted adapter, a routing function replaced on the class or the object), that export sends nothing and fails, with an error log naming the code `proxied`. Breaking: a plain channel with a session that has a hook, or while a library replaces the routing functions of `requests` (a `requests` test double), raises `proxied`; remove the hook, pass no session, use TLS, or keep the double away from a plain Future AGI channel. An https channel is unchanged.
- On a plain channel that carries Future AGI keys, what is checked is: the session's `proxies`, `trust_env` and the proxy environment; its hooks, read from a plain `dict` by key; each mounted adapter, by exact type (`requests.adapters.HTTPAdapter` itself, with an integer or `urllib3.util.Retry` itself as `max_retries`, and any pool sizes), in an `adapters` mapping that is a plain `dict` or `OrderedDict` read by direct iteration; `proxies` as a plain `dict` that holds nothing; the routing functions of the session and its adapters; and, on every export, that the redirect guard on the sending session is the one this package installed. Wrapping `urllib3` (`PoolManager.urlopen`, `HTTPConnectionPool.urlopen`) or `socket.create_connection` by calling through is not refused. The limit: patched library internals (functions of `requests` or `urllib3` replaced on the class after this package was imported, other than the routing functions listed) are out of scope and are not detected. Breaking: any `HTTPAdapter` subclass mounted on a session used for a plain-http Future AGI channel is refused with the code `proxied`, at construction and at export. That covers an adapter that overrides `init_poolmanager` (the ssl-context recipe, `TCPKeepAliveAdapter`, `SourceAddressAdapter`) and `HostHeaderSSLAdapter`. A `requests_futures.FuturesSession` is refused because it overrides `request`, `send` and `prepare_request`. To send on such a channel, use https, or mount `requests.adapters.HTTPAdapter` itself on a plain `requests.Session` (not a `requests_futures.FuturesSession`).
- On a plain channel that carries Future AGI keys the exporter sends with `requests.Session` itself, on OpenTelemetry 1.29 and 1.45: a method of a `requests.Session` subclass you pass (`post`, `get` or another verb) is not used to send, whatever it does to the URL or the headers. The sending session keeps the settings that are checked: `verify`, `cert`, `headers`, `max_redirects`, `auth`, `params`, cookies and adapters (the same settings). A subclass that replaces a routing function is refused with the code `proxied`. A channel with TLS sends with the session you passed, as you built it. Breaking: a `requests.Session` subclass whose `post` or another verb adds headers or changes the request is not used to send over a plain-http Future AGI channel; use https, or set the headers in the session's `headers`.
- On a plain channel that carries Future AGI keys, each adapter of the sending session is a new `requests.adapters.HTTPAdapter` built from plain settings: `pool_connections`, `pool_maxsize`, `pool_block` and `max_retries`, which is an integer or exactly `urllib3.util.Retry` (an integer or a `Retry` keeps its `total`, `connect`, `read`, `redirect`, `status`, `other`, `backoff_factor`, `allowed_methods`, `status_forcelist` and `raise_on_*` values). A `max_retries` that is a subclass of `urllib3.util.Retry` raises `proxied` when the exporter is built. Breaking: an adapter on a plain-http Future AGI channel whose `max_retries` is a `Retry` subclass is refused with the code `proxied`; use https, or pass an integer or `urllib3.util.Retry` itself. An attribute your adapter holds on the object (a replaced `build_connection_pool_key_attributes`, `poolmanager` or `proxy_manager`) is not carried to the sending adapter. The sending session is judged again immediately before each send: its `proxies`, `trust_env`, hooks, routing functions and redirect guard, and on every mounted adapter a pool manager that is exactly `urllib3.PoolManager` and no proxy manager. When it fails, nothing is sent and the export returns a failure with an error log naming the code `proxied`; an export answered with a redirect also returns a failure. A cookie jar from the standard library (`http.cookiejar.CookieJar`) on your session is copied to the sending session. Breaking: a custom `auth` callable, or `auth` given as a list, raises `proxied` on a plain channel; pass `auth` as a `(user, password)` tuple or `requests.auth.HTTPBasicAuth`, or use https. Breaking: a routing function replaced on the session or adapter object itself (an instance-level `send`, `get_adapter`, `get_connection` and the others listed above) raises `proxied`; remove the replacement, use https, or pass a plain `requests.Session`. Breaking: a subclass of `requests.Session` that overrides a routing function raises `proxied`; use https, or pass a plain `requests.Session` with `auth` as a tuple or `HTTPBasicAuth`.
- A request that carries Future AGI keys follows no redirect: the exporters, the eval requests and the `fi-instrument` collector check do not follow a `3xx`, and the request ends as a failure with no further request. Requests to your own collector are unchanged. Breaking: a Future AGI or self-hosted endpoint that answered with a redirect is not followed; set `FI_BASE_URL`, `FI_TRACES_URL` or `FI_GRPC_URL` to the address the redirect pointed to.
- `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` that carry an `x-api-key` or `x-secret-key` header (any case, with `_` read as `-` as servers that fold them do (`x_api_key`), including a name joined to its value by an encoded `=` such as `x-api-key%3D...` or `x-api-key+%3D+...`, or a pair carried inside another header's value such as `a=b%2Cx-api-key%3D...`, decoded up to eight times) raise `UnsupportedFutureAGIEndpointError` with the code `key_header_in_environment` when an exporter is built for an endpoint that is not Future AGI's, because OpenTelemetry sends those headers to that endpoint. The error names no value, and the environment value is read without any log record. Breaking: `key_header_in_environment` is raised for such a header; remove the key header from the environment, or set it on the exporter that should send it.
- `UnsupportedFutureAGIEndpointError` has a `code` attribute: `disputed_authority`, `empty_authority`, `insecure_futureagi_http`, `insecure_futureagi_grpc`, `insecure_self_hosted_target`, `proxied`, `unverified_endpoint` or `key_header_in_environment`. A URL or target with an empty authority (`https:///host/`, `http://:4318/`) is refused with `empty_authority`. Breaking: a URL or target with an empty authority raises `empty_authority`; write the host in the URL (`https://collector.example:4318/`).
- The custom eval template request, the eval-config request and the `fi-instrument` collector check follow the same endpoint rules as the exporters before any request is sent: an endpoint that names Future AGI without being verified raises `UnsupportedFutureAGIEndpointError` (it names `FI_BASE_URL`), and your own collector receives no Future AGI keys.
- A URL or target whose authority parsers can read differently is refused instead of read by one parser: whitespace or control characters in it, a backslash, userinfo or an `@`, an escaped backslash or `@`, an empty authority followed by a path (`https:///host/`), or a host that `urlsplit`, `urllib3` and the written text read differently. When it names a futureagi.com host, or is the endpoint `FI_BASE_URL`, `FI_TRACES_URL` or `FI_GRPC_URL` names, constructing the exporter (or calling the eval-config functions) raises `UnsupportedFutureAGIEndpointError` before any request. Keys in `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` count as Future AGI keys in that check. Breaking: such a URL raises `disputed_authority`; write the endpoint without userinfo, backslash or escaped delimiters (`https://host:port/path`).
- Only the host decides whether an endpoint is Future AGI's. A path, query, fragment or host name that contains `futureagi` (for example `futureagi-collector.corp`) is your own collector and receives no Future AGI keys. A futureagi.com host in another form (another port, a trailing dot, a full-width or `xn--` spelling, or `http://` on HTTP) raises `UnsupportedFutureAGIEndpointError` when Future AGI credentials are set; it names `FI_BASE_URL` and the endpoint option and echoes no value. Breaking: such an endpoint raises `unverified_endpoint`; set the https Future AGI URL, or remove the credentials.
- A gRPC target on a futureagi.com host uses the port `FI_GRPC_URL` names for that host, otherwise 443, whether it comes from an endpoint argument or from `FI_GRPC_URL`.
- A `requests.Session` passed to an exporter that sends to Future AGI is left as you built it: the Future AGI headers are set on a copy, which keeps the session's class when it can be created without arguments (a plain channel sends with `requests.Session` itself) and has its own adapters (same settings and TLS configuration), so closing the exporter does not close your connection pools.
- `urllib3` (`>=1.26.0,<3`, the range `requests` already requires) is a declared dependency, and `opentelemetry-api`, `opentelemetry-sdk` and `opentelemetry-instrumentation` are bounded together (`<1.46`, `<1.46`, `<0.67`) so a clean install passes `pip check` on every supported release.

## [1.5.1] - 2026-11-19

### Changed
- Zero-code auto-instrumentation turns on the `deepseek`, `fireworks`, `xai`, `minimax` and `vllm` integrations by default, and every model call through an OpenAI-compatible client yields exactly one span: the xAI and MiniMax integrations now defer to the `openai` integration while it is on (as the other three already did), and MiniMax hosts are labelled `minimax` on the shared spans. Each of these five turns on by default only when the installed provider package declares `DEFERS_TO_SHARED_OPENAI` (and the installed `traceai-openai` has the capabilities it needs); with an older provider package it keeps its previous state, so one span per call holds with any mix of installed versions. Uninstrumenting at shutdown runs in the reverse of the installation order, so each wrapper restores the callable it captured.
- Raw input and raw output (`raw.input`, `raw.output`, and their children) are not recorded while any of `hide_inputs`, `hide_outputs`, `hide_input_messages`, `hide_output_messages`, `hide_input_text`, `hide_output_text`, `hide_input_images`, `hide_embedding_vectors` or `hide_llm_invocation_parameters` is on.

### Fixed
- Trace export to Future AGI sends the Future AGI keys and the headers passed to register(); OTLP headers from the environment (OTEL_EXPORTER_OTLP_TRACES_HEADERS, OTEL_EXPORTER_OTLP_HEADERS), which are meant for other backends, are not included. An exporter you point at your own collector keeps standard OpenTelemetry environment behaviour.
- The Future AGI keys from `FI_API_KEY` and `FI_SECRET_KEY` are added only to exporters that send to Future AGI; an exporter you point at another collector sends the headers you pass.
- An exporter bound for Future AGI whose OpenTelemetry exporter release keeps its headers in a layout this release cannot set raises `UnsupportedOTLPExporterError` instead of sending.
- The OTLP exporter dependencies (`opentelemetry-exporter-otlp-proto-http`, and `opentelemetry-exporter-otlp-proto-grpc` for the `grpc` extra) are bounded to the versions this release is tested with, below 1.46.

## [1.5.0] - 2026-11-18

### Added
- `FI_TRACES_URL` sends span exports to a trace collector separate from the platform API (#710). Self-hosted Docker installs serve them on different ports (often 8100 for the API and 4318 for traces), so an app can set `FI_TRACES_URL` to the traces address while `FI_BASE_URL` and the `fagi` CLI keep using the API. When it is unset or blank, exports go to `FI_BASE_URL` as before; custom eval API calls always use `FI_BASE_URL`.
- Sampled spans on agent-created providers carry the trace's keep probability as the numeric `fi.sampling.ratio` attribute when it is strictly between 0 and 1, and a local fractional decision writes `fi=r:<ratio>` in W3C tracestate so services downstream inherit it (#713). Parent-based samplers copy a sampled parent's valid ratio at any local root rate. An absent attribute means `1.0`. Roots decided at rate 1, dropped or record-only spans, explicit custom samplers and application-owned providers get no sampling metadata.

### Fixed
- Agent-owned providers honour `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` when no explicit sample rate is set (#711). Explicit rates still win; invalid sampler settings warn once and use the parent-based default. Application-owned providers keep their sampler unchanged.

## [1.4.0] - 2026-11-17

### Added
- Zero-code auto-instrumentation. Start an unchanged app with `FI_AUTO_INSTRUMENT=true python app.py` (or under `opentelemetry-instrument`), or call `fi_instrumentation.init()` once in code: every installed traceAI integration turns on when the app first imports its library, and Future AGI receives only AI spans while your other OpenTelemetry exporters keep every span.
- `fi-instrument` command: `fi-instrument doctor` shows which libraries are covered, and `fi-instrument bootstrap --install` adds the missing traceAI packages.

## [1.3.2] - 2026-11-12

### Fixed
- using_session, using_user and the other context helpers keep the session and user on spans inside async functions and async generators when used as decorators.
- Generator functions traced with tracer.chain, tracer.agent or tracer.tool, sync or async, keep their span open until the generator finishes or is closed, so spans created inside them nest under it and the span's output defaults to the last yielded value.
- Eval tags for custom evaluations with no model set no longer make register fail.
- The get_session_attributes and get_context_attributes helpers set session.id alongside gen_ai.conversation.id.

## [1.3.1] - 2026-11-10
### Fixed
- PII redaction (`pii_redaction` / `FI_PII_REDACTION`) no longer corrupts correlation identifiers (#195):
  `session.id`, `user.id` and conversation ids are exported unchanged, numbers inside a longer
  identifier are no longer read as phone numbers, and card numbers must be card-shaped and pass the
  Luhn check. Free text and metadata are still redacted when the flag is on; hide flags and the image
  cap apply to every field.
- With `FI_PII_REDACTION` on, a phone number that follows a JSON escape (`\n`, `\t`, `\r`) in serialized
  text is now redacted; digits inside longer identifiers stay intact.
- Exporter credentials (#155): an empty `Authorization` header no longer hides valid
  `FI_API_KEY`/`FI_SECRET_KEY` environment keys, and explicit `X-Api-Key`/`X-Secret-Key`
  headers are sent once instead of alongside case-variant copies from the environment.
  Explicit credential headers still take precedence; header names are sent lower-case.
  Blank credential headers are skipped before case variants are merged.
  Known limitation: with `opentelemetry-exporter-otlp-proto-http` 1.45 or later, a blank
  `Authorization=` entry in `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` is
  still sent as an empty header next to the FI keys, because the exporter re-reads those variables.
  Remove blank entries from the environment; headers passed in code are unaffected.

## [1.3.0] - 2026-11-07
### Added
- Semantic conventions for agent cost, time to first token and coding-agent spans in
  `fi_instrumentation.fi_types` (#605): `tool_call.id`, `tool.name`, `fi.usage.*`, `fi.cost.kind`
  and the `fi.agent.*` attributes.

### Changed
- **The base64 image cap now defaults to 0** (it was 32000). `base64_image_max_length` and
  `FI_BASE64_IMAGE_MAX_LENGTH` keep working: set a positive value to export image payloads up
  to that many characters.
- Default cap 0 redacts any value or token that mentions a data: image URL, including mentions with no payload.
- The replacement is `[image-redacted]`, for the whole value or JSON string token.
- At the default cap 0, a non-URL value in an image payload position (typed image attributes,
  and the payload fields of image content parts) is replaced with `[image-redacted]`, even when it
  is short or is already a `__REDACTED__` marker. URL references in image content parts, such as
  `https://...`, are kept.
- An invalid, negative or non-integer cap resolves to 0 and logs
  `Invalid base64 image cap; using 0.` instead of raising.
- The TypeScript SDK is stricter: it never exports base64 images at any cap.

### Known limitations
- `fi-instrumentation-otel` currently requires `wrapt>=1.15.0,<2.0.0`; environments that require `wrapt>=2` cannot satisfy this dependency.
- Headerless base64 images (PNG, JPEG, GIF, WebP without a data: URL) are redacted only when they are the whole value or a whole JSON string token; padded or embedded headerless runs are exported.
- A declared image payload whose base64 contains spaces is exported.
- An over-cap image payload stored under a key outside the payload fields (data, bytes, content, image, image_url, url, base64, b64_json) of an image-declaring object, without a data: URL, is not detected by the image cap (e.g. {"type":"image","mime_type":"image/heic","blob":"<base64>"}); other media policy may still withhold it.
- Residual with an explicit positive cap: a payload that becomes recognizable only after multi-pass decoding inside a JSON string token, after a precise redaction has made the scope strict JSON, can be exported. The default cap 0 is not affected.
- With an explicit positive cap, the precise recognizer keeps this residual.

### Notes
- `traceai-agno` needs `fi-instrumentation-otel>=1.3.0`; its dependency floor will be raised in the agno release that follows the core release.

## [1.2.2] - 2026-10-30
### Fixed
- Apply content privacy to creation attributes and links before OpenTelemetry SDK
  sampling, and to sampler-produced attributes before span processors run. Preserve
  the sampler's decision, trace state, attribute selection and ordinary retention
  limits; keep FI's priority only for a caller-declared span kind.
- Extend input/output hiding to known tool, image, retrieval, reranking, audio,
  voice and computer-use content fields and their descendants, including event,
  exception and link attributes. Keep non-content metadata available. Mixed-direction
  content is hidden when either input or output hiding is enabled. These controls
  cover known semantic keys, not arbitrary custom attributes or free-text secrets.
- `FI_HIDE_EMBEDDING_VECTORS` now also hides `gen_ai.embeddings.vectors` and its
  descendants without evaluating hidden lazy values.
- Restore nested and concurrent `suppress_tracing` contexts correctly, including
  asynchronous use and exception exits.
- Honor explicit positional and keyword HTTP/gRPC exporter endpoints and headers
  instead of overwriting them with environment defaults.

### Changed
- Provider initialization leaves application SIGINT and SIGTERM handlers untouched.
  Normal-exit cleanup is still registered; applications must call `provider.shutdown()`
  through their own shutdown lifecycle to export pending spans on SIGTERM or SIGINT.
  See [application-owned shutdown](README.md#application-owned-shutdown) for an example.

## [1.2.1] - 2026-10-26
### Fixed
- `register()` no longer marks its own exporter as a default that the next
  `add_span_processor()` call replaces. Before, adding any span processor to the
  provider `register()` returns (a console exporter, a second backend, or a
  framework's own processor, such as LiveKit's privacy filter) shut the Future AGI
  exporter down and removed it, so traces silently stopped reaching Future AGI.
  The same happened to a custom `register(span_exporter=...)` exporter.

### Changed
- `add_span_processor()` on the provider `register()` returns adds the processor
  next to `register()`'s exporter, as in upstream OpenTelemetry, instead of
  replacing it. Code that relied on it to swap out `register()`'s exporter now
  exports to both; pass `register(span_exporter=...)` to export somewhere else
  instead.
- `register(span_exporter=...)` followed by `add_span_processor()` keeps the custom
  exporter, which used to be dropped, so spans go to both.
- If you pointed a second OTLP exporter at Future AGI to work around the old
  behaviour (for example traceai-strands with `OTEL_EXPORTER_OTLP_*` set to the
  Future AGI endpoint), remove it, or every span arrives twice. traceai-strands
  0.1.1 does not add its exporter when it would send the same spans to the same
  traces URL with exactly the same header names and values as `register()`'s
  exporter; an extra or different header on either side keeps it.
- Not changed: a `TracerProvider()` constructed directly still replaces the default
  `SimpleSpanProcessor` added on construction on its first `add_span_processor()`
  call.

## [1.2.0] - 2026-10-18
### Changed
- **Behaviour change for users of the hide flags:** setting any of `hide_inputs`,
  `hide_outputs`, `hide_input_messages`, `hide_output_messages`, `hide_input_text` or
  `hide_output_text` (or their `FI_HIDE_*` env vars) now also hides error details.
  Error text is content: provider error bodies quote the prompt back and carry request
  and session ids, and tracebacks carry local paths. Failed spans keep status `ERROR`,
  `error.type` and `exception.type`, but the status has no description and exception
  events drop `exception.message` / `exception.stacktrace`. `error.message`,
  `exception.message`, `exception.stacktrace` and instrumentor keys ending in
  `.error.message` / `.error_message` are dropped. Set `hide_error_details=False`
  (or `FI_HIDE_ERROR_DETAILS=false`) to keep the old behaviour.
- An `ERROR` status description is now the exception message only. The
  `"<Type>: <message>"` form built by OpenTelemetry's `use_span` and most instrumentors
  becomes `<message>` (the type is in `error.type`), also when one such line is joined
  per exception (llama-index), and a Python traceback is never kept in the description.
  The cleanup uses plain string operations, so a multi-megabyte exception message adds
  about a millisecond to `set_status` (no regular expression is built from message text).

- **Content-hiding fix:** `hide_inputs` (`FI_HIDE_INPUTS`) now also redacts `raw.input`
  (the raw request body, messages included) and the prompt template variables
  (`llm.prompt_template.variables`, `gen_ai.prompt.template.variables`), and
  `hide_outputs` (`FI_HIDE_OUTPUTS`) also redacts `raw.output`. Before, an instrumentation
  that set these kept the prompt in the stored row while the hide flag was on. Same keys
  as `@traceai/fi-core` 1.1.0.

### Feature
- New `TraceConfig(hide_error_details=...)` / `FI_HIDE_ERROR_DETAILS`. Unset, it is
  derived from the content hide flags above; an explicit `true`/`false` wins.
  `dataclasses.replace(config, hide_inputs=True)` derives it again from the new flags
  unless it was set explicitly.
- Spans that end with status `ERROR` get `error.type` (an exception recorded on a span
  that still ends OK, such as a recovered retry, does not set it). It is the provider
  error code for exceptions from known provider SDKs: openai and groq
  `code`/`type`, anthropic `error.type`, botocore/boto3 `Error.Code`, google-genai
  `status`, google-api-core gRPC status, mistralai body `type`, and the HTTP status for
  httpx `HTTPStatusError`. Every other exception gets its fully qualified class name
  (for example `sqlalchemy.exc.OperationalError`, never its `.code`). An instrumentor's
  own `error.type` is not overwritten. A failed span whose status is not about a
  named exception (for example `set_status(ERROR, "guardrail rejected")` after a retried
  timeout, outside an `except` block) gets `_OTHER` (OpenTelemetry semantic conventions)
  instead of a stale recovered exception. litellm exceptions carry no `code`/`type`, so
  they get their class name (for example `litellm.exceptions.NotFoundError`).

### Fixed
- `with tracer.start_span(...) as span:` now yields the FI span wrapper instead of the
  raw SDK span, so attribute masking, span-kind ordering and the error handling above
  also apply inside that block.
- `fi_instrumentation.instrumentation.version.__version__` now matches the package
  version (it still said `0.1.19`).

## [1.1.0] - 2026-08-11
### Feature
- `register()` now accepts span-processor / exporter / provider tuning:
  `max_queue_size`, `schedule_delay_millis`, `max_export_batch_size`,
  `export_timeout_millis`, `timeout`, `sampler`, `span_limits`, `span_exporter`.
  Unset values fall back to the OTEL env vars / upstream defaults (no behavior change).
- `BatchSpanProcessor` forwards its documented tuning params to the OpenTelemetry
  base class (previously silently dropped, causing unavoidable span drops under load).
- Re-exported `Sampler`, `ParentBased`, `TraceIdRatioBased`, `SpanLimits`.
### Fixed
- `register(batch=False)` warns when batch-only tuning params are passed.
- `register(span_exporter=...)` no longer crashes with non-OTLP exporters.
- Corrected docstrings that advertised an unsupported `endpoint` argument.

## [0.1.7] - 2025-06-10
### Feature
- Added support for ai-evaluation
- Added support for new evals in Prototype

## [0.1.6] - 2025-06-03
### Feature
- Added Support for Custom Evaluations in Prototype

## [0.1.5] - 2025-05-23
### Feature
- Added support for FutureAGI's protect
- Handling for incomplete spans during termination

## [0.1.4] - 2025-05-08
### Feature
- Bug fixes
- Support for new evals in Prototype

## [0.1.3] - 2025-04-14
### Feature
- Validations for Prototype eval mapping
- Added Support for Audio Evaluations