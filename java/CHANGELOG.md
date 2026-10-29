# Java SDK changelog

## Unreleased

- Prepare Maven Central coordinates under `com.futureagi`; Java packages remain `ai.traceai.*`.
- Add a BOM covering all instrumentation modules and an opt-in release profile for sources, Javadocs, signing, and Central publishing.
- Read the runtime SDK version from the parent Maven version instead of a separate hard-coded value.
- Keep commit-pinned JitPack builds and add Java reactor CI, Central metadata checks, and a one-request OpenAI smoke example.
- Correct installation and Future AGI authentication examples. No artifacts are published by this change.
- Install self-contained POMs only for JitPack so commit-pinned modules and the BOM do not refer to an unavailable remapped parent.
- Keep reactor test scopes on actual dependencies rather than leaking them through the public BOM.
- Clarify that Spring Boot's starter is additional to the direct OpenAI instrumentation; compile external README, Spring Boot, and BOM consumers in CI.
