# Changelog

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
