## [0.1.3] - 2026-10-18
### Fix
- A failed run's span status description is now the error message only. It was `run.error`, which LangChain builds as the message plus the full stack trace (including local file paths). The status is built from the error object passed to `handleLLMError` / `handleChainError` / `handleToolError` / `handleRetrieverError`; the stack trace stays only in `exception.stacktrace` on the exception event.
- Error spans get `error.type` (provider error code or error class) and follow `hideErrorDetails` (requires `@traceai/fi-core` 1.1.0).
- Depends on `@traceai/fi-core` 1.1.0, which also hides `llm.prompt_template.variables` with `hideInputs` and masks attributes passed at span creation. 0.1.2 was published against `@traceai/fi-core` `^0.1.16`.

### Behaviour change
- With any content-hiding flag on (e.g. `hideInputs` / `FI_HIDE_INPUTS`), error spans have no status description, and the exception event has no `exception.message` or `exception.stacktrace` (it keeps `exception.type`). Set `hideErrorDetails: false` to keep the details.
