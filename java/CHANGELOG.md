# Changelog

## Unreleased

### Changes

* **javaagent:** the agent does not register a global OpenTelemetry; application code that uses `GlobalOpenTelemetry` keeps its own provider (or the no-op default).
* **langchain4j, spring-ai:** add `TracedToolExecutor` and `TracedToolCallback`, which record one `execute_tool <name>` TOOL span per tool execution (LangChain4j AiServices tools, Spring AI `ToolCallback.call`, both overloads) with the tool name, call id where the framework provides it, description, arguments (`input.value`) and result (`output.value`). The hide settings apply to arguments and results, and a failure is recorded and rethrown unchanged.
* **javaagent:** the agent adds the same `execute_tool` span, with no code change, for classes that implement LangChain4j `ToolExecutor` or Spring AI `ToolCallback`. `FI_DISABLED_INSTRUMENTATIONS=langchain4j` or `spring-ai` turns each off. It does not advise the traceai libraries' own `TracedToolExecutor` and `TracedToolCallback` (any `ai.traceai` class except the agent's), and a call under a TOOL span for that tool or while Spring AI's own tool-calling observation for that tool is open (read from the application's current Micrometer observation, reflectively) is not traced a second time, and neither is an application subclass of a traceai wrapper. A call to another overload of the same tool callee with the same arguments (`call(String)` delegating to `call(String, ToolContext)`) is one execution; a tool that calls itself is traced at every level. The tool name (`gen_ai.tool.name`) is not withheld by the hide switches; the description, arguments and result are. With wire capture off, the `langchain4j` and `spring-ai` integration status no longer says chat calls go through wire.
* **java-core, langchain4j, spring-ai:** add `FITracer.setContentError`; `TracedToolExecutor` and `TracedToolCallback` use it, so any content hide setting (`hideInputs`, `hideOutputs`, `hideInputMessages`, `hideOutputMessages`), as well as `hideErrorDetails`, keeps the exception message and stack trace out of a tool span, as the agent does.
* **java-core:** `TraceAI.init` registers the configured `apiKey` and `secretKey` as known secrets. The exporters TraceAI creates for Future AGI and the console replace every occurrence of a known secret in span names, string and string-array attributes, event names and attributes, link attributes and status descriptions at export time, so a secret learned after a span was captured is still replaced. Overlapping or touching secrets are replaced as one region, and a secret that straddles a content length cap in the TraceAI instrumentations (core, Bedrock, Semantic Kernel, Spring AI) is replaced before the value is cut, or the text kept before the cut loses its last characters and ends with `...[truncated]`, so no part of it remains, including when the cut text sits inside a JSON attribute: every `...[truncated]` marker in a value is a cut point at export, and a secret learned after the capture loses the start of it that precedes any marker. A known secret is also replaced where it appears JSON-escaped (quotes, backslashes and control characters) in a JSON-encoded attribute. Spring AI message JSON writes `=`, `&`, `<`, `>` and `'` unescaped, so secrets containing them are replaced. Resource and instrumentation scope attributes, names and versions go through the same replacement; only string and string-array values are rewritten. Spans keep their raw values. Exporters on an OpenTelemetry SDK you own (`TraceAI.init(config, openTelemetry)`) are not reached; wrap them with `SecretReplacingSpanExporter.wrap` to get the same replacement. Values under 8 characters are not registered (one warning per process says so, without the value). The registry holds at most 1024 values and 4 MiB of characters: beyond that it fails closed, the wrapped exporter drops every span, and one warning per process names the bound. A value too large to check against every secret is exported as `<redacted>`.
* **java-core, spring-ai, semantic-kernel:** a region replaced at export never splits a JSON escape: a region that would start or end inside one (`\n`, `\t`, `\"`, `\uXXXX`) takes the whole escape, so exported JSON attributes parse and no secret remains in the raw or the decoded value, with one exception: a value too large for the replacement work bound is replaced whole by `<redacted>`, which is not JSON. An escaped surrogate pair is replaced as one unit. `KnownSecrets.cap` and the new `KnownSecrets.sealCut(text, limit)` return at most `limit` characters (`sealCut(text)` without a limit has no fixed growth bound); Spring AI's message capture probes and seals each field with one registry snapshot. The deprecated library-info view of a rewritten instrumentation scope keeps its schema URL. Not matched: a secret written as `\u2028` or `\u2029`, and a secret inside double-encoded JSON.
* **java-core, javaagent:** plaintext `http://` to a Future AGI host is refused, and plaintext `http://` to any other host except literal `localhost`, `127.0.0.1` and `::1` is refused unless `FI_ALLOW_INSECURE_HTTP=1` (or `TraceConfig.builder().allowInsecureHttp(true)`) is set; the agent reads that setting from the environment and its explicit option only, not from `futureagi.toml` or profile files. Export starts only when both `FI_API_KEY` and `FI_SECRET_KEY` are set. A target with an `@`, a backslash or an escaped backslash anywhere after the host is refused, and an IPv6 zone (`http://[fe80::1%25eth0]:4318`) is read as the host it names and is never loopback. `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` that name an `X-Api-Key` or `X-Secret-Key` header (any case, percent-decoded, in any comma-separated pair) or carry a Future AGI key value refuse the configuration; another vendor's keys in them are not judged. Key-carrying requests do not follow redirects, and a plaintext send does not use a proxy.
* **java-core:** `TraceAI.init` throws `KeyTransport.Refused`, whose message names no value (and names `FI_ALLOW_INSECURE_HTTP` when plaintext is the reason), for a configuration the rules above refuse, such as `http://collector:4318`. The Spring Boot starter adds `traceai.allow-insecure-http` (the explicit opt-in) and `traceai.fail-on-refused-transport` (default `true`; `false` logs one value-free warning and disables export instead of failing the context: no exporter is created and neither `FI_BASE_URL` nor the keys are read again, so nothing is sent to any target, an approved `FI_BASE_URL` included). `TraceConfig.builder().exportEnabled(false)` is the same switch for code: no exporter, and the base URL and keys stay unset. The agent's explicit `allow_insecure_http` option is a boolean `true` or the text `1` or `true`; the environment variable is exactly `1`. A keyed exporter ends a send before its result completes, so `shutdown()` called from a completion callback returns without waiting out its own send.
* **java-core, javaagent:** the exporter that sends keys differs from `OtlpHttpSpanExporter` in these ways: a failed batch is dropped, with no retry and never to another target; a 3xx answer fails the batch; requests are not compressed; the endpoint is not logged; `flush()` and `shutdown()` wait up to a limit for a send in flight and report failure while one is still running; and after `shutdown()` the exporter sends nothing and reports failure on each export. The javaagent checks the target and the ambient headers once at startup: a refusal logs one warning that names no value and installs no exporter.
* **javaagent:** Google ADK for Java (1.0.0 to 1.15.0) is traced with no code: invocation (`CHAIN`), agent run (`AGENT`), model call (`LLM`, with model, provider, messages including tool calls and results, tool definitions and token counts) and tool call (`TOOL`, with name, description, arguments and result), nested as ADK nests them, through the agent's route with its `FI_HIDE_*` switches and known-secret replacement. It works with or without an OpenTelemetry SDK in the application, leaves the application's own ADK spans unchanged, does not trace a model's HTTP call a second time while an ADK model call is in flight (whichever thread the model client sends it from; a request from a thread with no span of its own is attributed to the call in flight), is gated only on the agent and `google-adk` being enabled and not on the wire switches, shows in `doctor` as `google-adk`, and `FI_DISABLED_INSTRUMENTATIONS=google-adk` turns it off.
* **google-adk:** new module `traceai-java-google-adk` for a manual setup: `TracedLlm` and `TracedTool` wrap an ADK `BaseLlm` and function `BaseTool` and record LLM and TOOL spans through `FITracer`; `TracedTool` forwards `processLlmRequest` and the custom metadata to the wrapped tool, so a wrapped built-in tool still shapes the model request. The agent jar relocates its copy of the attribute mapping, so it cannot clash with this module.

## 1.4.1 (2026-11-19)

### Changes

* **java-core:** raw input and raw output are not recorded while any content hide setting is on.
* **java-core:** number and boolean values (and arrays of them) written through `FITracer` on content keys are masked by key under the hide settings, like text values.
* **cohere:** tool-call arguments go through the masker; chat, embed and rerank raw responses are not recorded while any content hide setting is on.
* **ollama:** the raw chat response is not recorded while any content hide setting is on.
* **langchain4j:** AiServices method arguments (`langchain4j.param.*`) are not recorded under `hideInputs` / `hideInputMessages`.
* **javaagent:** the agent masks the same content keys as `traceai-java-core`, including number and boolean values; it does not record a whole provider response body while any content hide switch is on.
* **java-core, javaagent:** a whole-number document count on the reranker document keys (`gen_ai.reranker.input_documents`, `gen_ai.reranker.output_documents` and the `reranker.*` names) is kept under every hide setting; text on these keys, and keys below them, is masked.
* **javaagent:** an application span's raw input and output (`raw.input`, `raw.output`, `fi.raw_input`, `fi.raw_output`) are not exported while any content hide switch is on, including `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES` and `FI_HIDE_LLM_INVOCATION_PARAMETERS`.
* **java-core:** under `hideInputMessages` / `hideOutputMessages`, `input.value` / `output.value` and the raw input/output (`raw.input`, `fi.raw_input`, `raw.output`, `fi.raw_output`) are redacted; embedding input text, and tool-call arguments nested in message keys, are masked as well. `maskAttribute` drops error-detail keys under `hideErrorDetails`. The Azure OpenAI, LangChain4j, Semantic Kernel and Bedrock instrumentations write these attributes through the masker ([#737](https://github.com/future-agi/traceAI/pull/737)).
* **java-core:** with the console exporter enabled, `TraceAI.shutdown()` completes promptly while spans end concurrently with it. Exporter exceptions are logged at WARNING and failed exports at FINE ([#738](https://github.com/future-agi/traceAI/pull/738)).

## 1.4.0 (2026-11-19)

### Features

* **javaagent:** join the caller's W3C trace on servlet requests. While `HttpServlet#service` (or an override; `jakarta.servlet` and `javax.servlet`, which includes Spring MVC) handles a request, its `traceparent` and `tracestate` headers are the parent of the AI calls made on that request's thread, so a sampled caller gets the AI spans in its own trace with its `fi=r:` ratio inherited, and an unsampled caller gets nothing exported under the default parent-based sampler. No SERVER span is created, your own active span still takes precedence, and `FI_DISABLED_INSTRUMENTATIONS=servlet` turns it off. Like the rest of the agent, it stays off when the OpenTelemetry Java agent is also running ([#724](https://github.com/future-agi/traceAI/pull/724)).

### Bug Fixes

* **java-core:** a partial ratio decision always puts the `fi` tracestate entry at the left of the W3C tracestate, including when it replaces an existing `fi` entry, and adding `fi` to a full (32-member) tracestate evicts the right-most member ([#719](https://github.com/future-agi/traceAI/pull/719)).

## 1.3.0 (2026-11-18)

### Features

* **java-core, javaagent:** honor `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` in core-owned and auto-agent providers when no explicit `FI_SAMPLE_RATE` / `sample_rate` is set; explicit rates use parent-based trace-id sampling. Add `TraceConfig.Builder.sampler(Sampler)` for manual initialization without changing application-owned providers. Invalid standard settings warn once and use the parent-based rate-one default; parent-based choices now respect unsampled application parents ([#711](https://github.com/future-agi/traceAI/pull/711)).
* **java-core, javaagent:** on TraceAI-owned providers, sampled spans carry the double attribute `fi.sampling.ratio` for a known partial keep probability (`0 < r < 1`), and partial ratio decisions write `fi=r:<ratio>` in W3C tracestate, replacing an existing `fi` entry even in a full tracestate. Parent-based children copy a valid ratio from a sampled local or remote parent's `fi` entry, at any root rate. An absent attribute means `1.0`. Roots at rate `1`, dropped or record-only spans, unknown custom samplers and application-owned providers are not stamped ([#713](https://github.com/future-agi/traceAI/pull/713)).

## 1.2.0 (2026-11-17)

### Features

* **javaagent:** first release of `com.futureagi:traceai-javaagent`, the zero-code Java agent. Add `-javaagent:traceai-javaagent-<version>.jar` (or call `TraceAI.init()`) to trace AI provider HTTP calls and send only those spans to Future AGI, leaving the application's OpenTelemetry setup untouched. The BOM now aligns it with the other modules.

## [1.1.0](https://github.com/future-agi/traceAI/compare/java-v1.0.3...java-v1.1.0) (2026-11-10)

### Features

* **java-core:** add Python-aligned `GEN_AI_*` names for every GenAI attribute in `SemanticConventions`. The 16 legacy `LLM_*` names stay as deprecated compile-time aliases with identical values, so existing source and binaries keep working; `LLM_SYSTEM` maps to `GEN_AI_PROVIDER_NAME` (`gen_ai.provider.name`) ([#654](https://github.com/future-agi/traceAI/pull/654), TH-3776).

## [1.0.3](https://github.com/future-agi/traceAI/compare/java-v1.0.2...java-v1.0.3) (2026-11-10)

### Features

* **semconv:** add cost, time-to-first-token and coding-agent attribute constants to `SemanticConventions` ([#605](https://github.com/future-agi/traceAI/pull/605)).

### Bug Fixes

* **java-core:** respect the `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES` and `FI_HIDE_OUTPUT_MESSAGES` environment flags in `TraceConfig` defaults; explicit builder settings still take precedence ([#543](https://github.com/future-agi/traceAI/pull/543)).
* **ollama-java:** restore legacy Ollama4j 1.0.79 generation with empty default options ([#425](https://github.com/future-agi/traceAI/pull/425)).

## [1.0.2](https://github.com/future-agi/traceAI/compare/java-v1.0.1...java-v1.0.2) (2026-11-05)


### Bug Fixes

* **azure-search-java:** preserve document privacy, payloads and errors ([#575](https://github.com/future-agi/traceAI/issues/575)) ([1851d3a](https://github.com/future-agi/traceAI/commit/1851d3a864b22db75c54f6b4388189021f166212))
* **bedrock-java:** close privacy, usage and stream tracing gaps ([#579](https://github.com/future-agi/traceAI/issues/579)) ([a9c29ad](https://github.com/future-agi/traceAI/commit/a9c29ad4d189ef9b1aa8484e081d234d4ae17ad1))
* **chromadb:** define the Java legacy tracing contract ([#342](https://github.com/future-agi/traceAI/issues/342)) ([9af12e9](https://github.com/future-agi/traceAI/commit/9af12e922a033ab7948cad8d9ddc3e520c101d10))
* **fi-core-java:** align README coordinates with available APIs ([#581](https://github.com/future-agi/traceAI/issues/581)) ([7fa0bef](https://github.com/future-agi/traceAI/commit/7fa0befc7cfae4851f5ab93f3eb0a055c858d73c))
* **java-anthropic:** document native server-tool billing limit ([#585](https://github.com/future-agi/traceAI/issues/585)) ([201c8bf](https://github.com/future-agi/traceAI/commit/201c8bf75a3cc250b6dbd59e3ef12a2196eceef4))
* **java-azure-search:** preserve lazy page retrieval ([#337](https://github.com/future-agi/traceAI/issues/337)) ([cdee70d](https://github.com/future-agi/traceAI/commit/cdee70dd76ff7ee00aaa69e0d6477485e7bd6925))
* **java-bedrock:** preserve profile parsing and structured tracing ([#343](https://github.com/future-agi/traceAI/issues/343)) ([943fbde](https://github.com/future-agi/traceAI/commit/943fbde0bcf39e2a87574ede0d5c0a993c57db71))
* **java-core:** maintain native Arconia error privacy in shared core ([#334](https://github.com/future-agi/traceAI/issues/334)) ([d6f3875](https://github.com/future-agi/traceAI/commit/d6f387548468bc9c1224c121a96feda7c891f41a))
* **java-core:** separate owned and borrowed provider lifecycles ([#362](https://github.com/future-agi/traceAI/issues/362)) ([b059859](https://github.com/future-agi/traceAI/commit/b059859f6575e7c9bf09afe512f020c6c42bfe0d))
* **java-weaviate:** finalize async spans before terminal continuations ([#527](https://github.com/future-agi/traceAI/issues/527)) ([f0766e0](https://github.com/future-agi/traceAI/commit/f0766e085c9eb9d81ba51ccdf4f55ef509dc6f4d))
* **java:** repair and qualify retained Anthropic wrapper contracts ([#534](https://github.com/future-agi/traceAI/issues/534)) ([340e90a](https://github.com/future-agi/traceAI/commit/340e90aab3581ef4da8d281ffbc8250e85255fbe))
* **semantic-kernel-java:** capture argument values without affecting calls ([#589](https://github.com/future-agi/traceAI/issues/589)) ([c4ac37f](https://github.com/future-agi/traceAI/commit/c4ac37f893df3e550d7a2708c7606f0958eecd7e))
* **semantic-kernel-java:** route subscription-scoped native telemetry ([#455](https://github.com/future-agi/traceAI/issues/455)) ([776c9c3](https://github.com/future-agi/traceAI/commit/776c9c3b051cf4d224570d55b9a37878ba4da4fa))
* **spring-ai:** close native tracing release qualification gaps ([#564](https://github.com/future-agi/traceAI/issues/564)) ([b5e1fcc](https://github.com/future-agi/traceAI/commit/b5e1fccb16405c7bc4d13ab359fcf42a6e36ae4d))
* **spring-ai:** retain handled tool failure outcomes ([#578](https://github.com/future-agi/traceAI/issues/578)) ([6b0e60d](https://github.com/future-agi/traceAI/commit/6b0e60dc7e39cb3a5fde32f4a627a549c23ca285))
* **weaviate-java:** redact legacy errors and report rejected imports ([#565](https://github.com/future-agi/traceAI/issues/565)) ([76a9dfc](https://github.com/future-agi/traceAI/commit/76a9dfc8391d509ece18d1d509d8c579cbf77a1b))

## [1.0.1](https://github.com/future-agi/traceAI/compare/java-v1.0.0...java-v1.0.1) (2026-11-01)


### Bug Fixes

* **quarkus-langchain4j:** package native tracing contributors ([#453](https://github.com/future-agi/traceAI/issues/453)) ([9090443](https://github.com/future-agi/traceAI/commit/909044347f394a7338edb91206bbaa818995dfc3))
* **redis:** preserve vector query and schema semantics ([#449](https://github.com/future-agi/traceAI/issues/449)) ([ac7c852](https://github.com/future-agi/traceAI/commit/ac7c85213b32e440271954895c6b20345453a2d9))
* **watsonx-java:** trace released IBM service APIs ([#468](https://github.com/future-agi/traceAI/issues/468)) ([e845393](https://github.com/future-agi/traceAI/commit/e845393109c9e41b6ad637650f348f912af3b907))

## Java SDK changelog

## Unreleased

- Prepare Maven Central coordinates under `com.futureagi`; Java packages remain `ai.traceai.*`.
- Add a BOM covering all instrumentation modules and an opt-in release profile for sources, Javadocs, signing, and Central publishing.
- Read the runtime SDK version from the parent Maven version instead of a separate hard-coded value.
- Keep commit-pinned JitPack builds and add Java reactor CI, Central metadata checks, and a one-request OpenAI smoke example.
- Correct installation and Future AGI authentication examples. No artifacts are published by this change.
- Install self-contained POMs only for JitPack so commit-pinned modules and the BOM do not refer to an unavailable remapped parent.
- Keep reactor test scopes on actual dependencies rather than leaking them through the public BOM.
- Clarify that Spring Boot's starter is additional to the direct OpenAI instrumentation; compile external README, Spring Boot, and BOM consumers in CI.
