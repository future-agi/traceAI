## [1.3.0] - 2026-11-12

### Changed
- ATTR_PROJECT_NAME now emits project_name instead of fi.project.name, and ATTR_SERVICE_NAME now emits service.name instead of SERVICE_NAME. Action: Update any resource lookups, processors or queries that read the old keys; traces already recorded keep the old keys.

## [1.4.0](https://github.com/future-agi/traceAI/compare/npm-traceai-fi-semantic-conventions-v1.3.0...npm-traceai-fi-semantic-conventions-v1.4.0) (2026-11-28)


### Features

* Skills runtime attribution for traces (TypeScript and Python) ([a8fc0ab](https://github.com/future-agi/traceAI/commit/a8fc0abccf525aab925da6d93d882cfa804e9449))
* **typescript:** send Future AGI keys only to allowed destinations over the SDK's own transport ([e84aa91](https://github.com/future-agi/traceAI/commit/e84aa91d39e049edbe4e0eaae5abbf94b321c27f))

## [1.2.0] - 2026-11-10
### Added
- Align GenAI constant names with Python's `SpanAttributes`: expose `GEN_AI_*` names as named exports and on `SemanticConventions`.
- Preserve existing `LLM_*` exports as deprecated aliases without changing attribute keys or literal types.
- Document canonical names and replace legacy `llm.*` keys in the usage example.
- Check every exported `gen_ai.*` value against Python's canonical names in CI, including changes to the Python reference.

## [0.1.8] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.7] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.6] - 2025-05-26
### Feature
- Bug Fixes

## [0.1.5] - 2025-05-26
### Feature
- Bug Fixes

## [0.1.4] - 2025-05-23
### Feature
- Dependencies Updated

## [0.1.1] - 2025-05-20
### Feature
- Dependencies Updated

## [0.1.0] - 2025-05-20
### Feature
- Initial Release
