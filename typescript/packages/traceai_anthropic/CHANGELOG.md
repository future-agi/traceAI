## [0.1.3] - 2026-10-18
### Fix
- Content hiding now applies to the prompt passed when the span is created (`gen_ai.input.messages`, `raw.input`), via `@traceai/fi-core` 1.1.0, which this release depends on. 0.1.2 was published against `@traceai/fi-core` `^0.1.13`, which exported those attributes unmasked.
- Error spans follow `hideErrorDetails` and get `error.type` (from `@traceai/fi-core` 1.1.0).

## [0.1.1] - 2025-06-10
### Feature
- Dependencies Updated


## [0.1.0] - 2025-06-10
### Feature
- Initial Release
- Added support for Anthropic



