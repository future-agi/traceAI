# Changelog

## [1.1.0](https://github.com/future-agi/traceAI/compare/nuget-TraceAI.AutoInstrumentation-v1.0.1...nuget-TraceAI.AutoInstrumentation-v1.1.0) (2026-11-28)


### Features

* **csharp-auto:** trace ASP.NET Core chat clients, embeddings and agents with no code changes ([6f3ccbd](https://github.com/future-agi/traceAI/commit/6f3ccbd3ec964e5c5325ee6d4bc637864588a1bf))
* **csharp:** one decision for every key-carrying request: TLS to Future AGI, plaintext opt-in, no proxy for plaintext, no redirects, ambient headers ([b1946b3](https://github.com/future-agi/traceAI/commit/b1946b3b4f89b54af6a7c176db2f1b02a789adea))
* **csharp:** record prompt and response content by default in ASP.NET Core hosts ([99a2714](https://github.com/future-agi/traceAI/commit/99a27142a4f61cf83783808a00236411c70eaa73))
* **csharp:** record prompt and response content by default in ASP.NET Core hosts ([0d82a1e](https://github.com/future-agi/traceAI/commit/0d82a1eb0315b766d1dc8b9f80aa06ee705dedbd))
* **csharp:** replace known secrets on both span copies ([0f87245](https://github.com/future-agi/traceAI/commit/0f872453996f31c6307b3c70a7120c8dc77d5336))
* **csharp:** send Future AGI keys only to allowed destinations ([a42d045](https://github.com/future-agi/traceAI/commit/a42d0456321975e3f490058d0313feed595d451b))


### Bug Fixes

* **csharp-auto:** factory registrations use the same ownership path as type registrations; free an unused wrapper; no generated types without dynamic code ([f5c76ce](https://github.com/future-agi/traceAI/commit/f5c76cee715e464c8619c7f68354f2c279a3a5de))
* **csharp-auto:** hosting startup skips open generics, leaves owned instances alone, keeps content off by default; wire its tests into the solution and CI ([66310aa](https://github.com/future-agi/traceAI/commit/66310aa287d0cc96371730cad81e4cfe81ea326f))
* **csharp-auto:** type registrations are built with ActivatorUtilities, no keyed or extra descriptors; the content variable counts as true only when it says true ([f1943b1](https://github.com/future-agi/traceAI/commit/f1943b10183accb496627d1cd918613e4875a1bc))
* **csharp-auto:** type registrations hand the container an owner that releases the wrapper and the implementation once, async when possible ([09e8154](https://github.com/future-agi/traceAI/commit/09e815444a1f26629a4b502376c7307f34c92c0a))
* **csharp-auto:** type registrations resolve a private keyed implementation with the original lifetime; the standard content variable is not overridden to true ([4576bc6](https://github.com/future-agi/traceAI/commit/4576bc6ff7ca4778023ca0b01b4eec36e42731be))
* **csharp:** a blank FI_* env value never overrides the file; keep TOML literal content switch; warn on unrecognised value; note content only for kept registrations; exact hide wording ([8936fd7](https://github.com/future-agi/traceAI/commit/8936fd708959e1a0cf30f50252dbbb0bcb163b5b))
* **csharp:** a blank FI_BASE_URL stops the agent, a blank FI_CONFIG_FILE reads no file and a blank FI_WIRE_CAPTURE is off; per-key table and exact docs ([6e1d540](https://github.com/future-agi/traceAI/commit/6e1d5405d9fd656388142da6090f6ebb535a92ba))
* **csharp:** FI keys and credential values stay pinned without a bound, the Future AGI copy's operation name is replaced from the unreplaced name, exact README and CHANGELOG ([f2eb482](https://github.com/future-agi/traceAI/commit/f2eb482a8c032e5258714608b29627cefaf15555))
* **csharp:** keep the most recent RegisterSecret values when the registry is full ([700781d](https://github.com/future-agi/traceAI/commit/700781dbd1999ca61339b341ec95ad8b134d40e8))
* **csharp:** keep wire-span values unreplaced for the Future AGI copy, replace overlapping known secrets as one span, take one snapshot per copy ([e060b09](https://github.com/future-agi/traceAI/commit/e060b09714b2f5cc94d0d2f9437fd2250317bdf4))
* **csharp:** no proxy for any plaintext key send, keep the exporter timeout, narrow the known-secret scope ([9673151](https://github.com/future-agi/traceAI/commit/967315165a26bdfc2046d33f3ffea260b8d96032))
* **csharp:** replace every secret the agent knows in request-context values on the spans the application's own providers see ([820c693](https://github.com/future-agi/traceAI/commit/820c693471a3a8a5fb5de5db7298291b8914ae56))
* **csharp:** replace known secrets before value shapes and caps, pin FI keys outside the LRU, keep unreplaced wire values off the Activity ([b81a3bb](https://github.com/future-agi/traceAI/commit/b81a3bb43f794e618efd65b36dc0aa741980ddac))
* **csharp:** screen ambient exporter headers on the unescaped text and at Register, refuse hosts whose Host and IdnHost differ, keep registered secrets in the shared state in recency order, exact messages and CHANGELOG ([61e8a7c](https://github.com/future-agi/traceAI/commit/61e8a7cb965061d78b4134e2a98e82aa4f40b39a))
* **privacy:** credential value shapes scan in linear time in every SDK, tokens replaced whole ([607ee0c](https://github.com/future-agi/traceAI/commit/607ee0c7658118db01601772a9d233bec59bbcbc))
* **privacy:** credential value shapes scan in linear time in every SDK, tokens replaced whole ([5defddd](https://github.com/future-agi/traceAI/commit/5defddd0dd3531d2e76d47c34075c14875ab8c22))

## Changelog

This package is versioned together with FIInstrumentation (`fi-instrumentation-otel`): the
startup hook embeds the SDK sources, so a change to either one releases both at one version.
Release notes are added here by the release process.

## Unreleased

### Changed
- Future AGI keys are sent only to a destination the agent allows. A Future AGI host (`*.futureagi.com`) is TLS only: plaintext http to it is always refused, and neither `FI_ALLOW_INSECURE_HTTP` nor `AutoOptions.AllowInsecureHttp` applies to it. Plaintext to a literal loopback host (`localhost`, `127.0.0.1`, `::1`) needs no setting. Plaintext to any other host is refused unless `FI_ALLOW_INSECURE_HTTP` is exactly `1` or `AutoOptions.AllowInsecureHttp` is set, which warns once per process naming the setting and the collector's `scheme://host:port`. A base URL whose host a second parser reads differently is refused: any backslash or `%5C` in the authority, an `@` in the path, query or fragment, an empty host, and a host name whose `Uri.Host` and `Uri.IdnHost` differ (non-ASCII letters, full-width dots). IPv6 zone literals (`[fe80::1%25eth0]`, `[::1%25lo]`) are not loopback: they need the setting like any other plaintext target. `init` logs a refused route with the setting to change and no value, and the application continues without tracing; it does not throw.
- A request that carries the keys never follows a redirect (a 3xx answer is a failed export and nothing goes to the address it names), and a plaintext one (loopback or opted in) is never sent through a proxy; an https export keeps the normal proxy behaviour. The export keeps its 2-second timeout.
- The agent does not start its own Future AGI exporter, or an `AutoOptions.Exporter` you supply, when `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` carries an `X-Api-Key` or `X-Secret-Key` header, or a value that contains a Future AGI key (from the options, the environment or the config file) or a secret registered with `RegisterSecret`. The text is checked as written and with every percent escape resolved, as the exporter resolves it. A header named `X-Api-Key` or `X-Secret-Key` disables it whatever its value, including another vendor's; any other header in those variables does not, unless its value contains one of those keys or secrets.
- Credential value shapes are scanned in time linear in the text, and a token is replaced whole whatever its length.

### Added
- ASP.NET Core hosts are traced with no application code: the startup hook activates the companion assembly `TraceAI.AutoInstrumentation.HostingStartup`, which wraps registered `IChatClient`, `IEmbeddingGenerator<,>` and `AIAgent` services in the libraries' own OpenTelemetry wrappers when `extensions_ai` or `agents` is enabled. An application that already calls `UseOpenTelemetry()` is not wrapped twice, no hide setting is overridden, and an existing `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES` value is kept.
- The agent starts next to `System.Diagnostics.DiagnosticSource` 11, which `Microsoft.Extensions.AI` 11 ships.

### Changed
- A known secret that contains a credential value shape (an `AKIA...` key followed by more characters, a `Bearer ...` value with a `~` or `.` suffix) is replaced whole on both copies of a wire span: known secrets are replaced first and value shapes run on the result, in the same call, never on raw text.
- The FI keys and credential values are pinned outside the registry's LRU, in a set that is not bounded, so registering more than 1,024 other secrets never removes them from either copy.
- The 500-character status description of a non-JSON error body is cut after replacement, not before. A multipart text field cut at 64 KiB also loses its last 4,095 characters, so no secret straddling the cap keeps a part of itself.
- Known secrets that overlap one another, or contain one another, are replaced as one span on both copies of the spans the agent creates for HTTP model calls, so no part of any of them remains, including in request-context values. The Future AGI copy is replaced at export from the registry as it is then, so a secret learned after a span was captured is replaced too; the application's own providers and exporters see the values replaced once, when the span ends. Replacement runs on the whole value before any length limit applies.
- The spans the agent creates for HTTP model calls carry the same known-secret replacement for the application's own providers, listeners and exporters as on the Future AGI copy: secrets in the process-wide registry (registered secrets and credentials discovered in requests) are replaced in request-context values as well as the FI keys, whether or not any `FI_HIDE_*` setting is on. Spans a library creates are not changed by the agent.
- Prompt and response content is recorded by default in ASP.NET Core hosts, like in every other language. Set `FI_AUTO_CAPTURE_CONTENT=false`, or any of `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` or `FI_HIDE_INPUT_IMAGES` set to `true`, to keep it off; a `TraceConfig` supplied to the agent with any of those hides keeps it off too, and is used instead of the variables. `FI_AUTO_CAPTURE_CONTENT` also accepts `'false'` as a TOML literal string in the config file, and an unrecognised value keeps content off with one warning that names only the setting. The libraries' switch puts content on their spans for every listener in the application, not only this agent; the agent prints one line when it first turns the switch on for a service it keeps (factory and type registrations print it on first resolve).
- A blank (empty or whitespace) `FI_*` environment value counts as not set, so the config file's value or the default applies, except where that would be the less cautious reading: a blank `FI_BASE_URL` stops the agent (one line names `FI_BASE_URL` and says traces are off; the application keeps running untraced), a blank `FI_CONFIG_FILE` reads no config file, and a blank `FI_WIRE_CAPTURE` is off. A blank `FI_SAMPLE_RATE` leaves `OTEL_TRACES_SAMPLER` in charge and a blank `FI_AUTO_DEBUG` lets the file decide. The README lists every key.
