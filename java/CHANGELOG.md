# Changelog

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
