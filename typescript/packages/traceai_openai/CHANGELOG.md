## [0.1.14] - 2026-10-18
### Fix
- Content hiding now applies to the prompt. With `hideInputs` / `FI_HIDE_INPUTS=true`, `input.value`, `gen_ai.input.messages` and `raw.input` were still exported, because they are passed when the span is created and `@traceai/fi-core` did not mask creation-time attributes. Fixed in `@traceai/fi-core` 1.1.0; this release depends on it (0.1.13 was published against `@traceai/fi-core` `^0.1.13`, which has the bug).
- Error spans follow `hideErrorDetails` and get `error.type` (from `@traceai/fi-core` 1.1.0).

## [0.1.10] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.9] - 2025-06-10
### Feature
- Bug Fixes

## [0.1.8] - 2025-06-10
### Feature
- Bug Fixes

## [0.1.7] - 2025-06-10
### Feature
- Bug Fixes

## [0.1.6] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.2] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.1] - 2025-06-10
### Feature
- Dependencies Updated

## [0.1.0] - 2025-06-10
### Feature
- Initial Release
- Added support for OpenAI