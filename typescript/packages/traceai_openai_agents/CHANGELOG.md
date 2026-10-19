## [0.1.1] - 2026-10-18
### Fix
- A failed span's status description is now the error message only. It was `"<message>: <data>"`, where `data` (e.g. a tool's error text) can quote content and printed as `[object Object]` for objects. `data` now goes to `exception.message` on an exception event, which is dropped when error details are hidden.
- Error spans get `error.type` and follow `hideErrorDetails` (requires `@traceai/fi-core` 1.1.0). The SDK's span errors have no class, so `exception.type` and `error.type` are `AgentSpanError`.
- With `@traceai/fi-core` 1.1.0, `raw.input` and the other content passed at span creation are masked by the hide flags.

### Behaviour change
- With any content-hiding flag on (e.g. `hideInputs` / `FI_HIDE_INPUTS`), error spans have no status description and no exception message. Set `hideErrorDetails: false` to keep them.

## [0.1.0]
- Initial release.
