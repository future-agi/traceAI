## [0.1.1] - 2026-10-18
### Fix
- Declare `@aws-sdk/client-bedrock-runtime` `^3.693.0` as a peer dependency, like the other instrumentors declare their SDK. The package loads it at runtime, so a clean install without it failed with "Cannot find module".
- A failure that is not an `Error` of this realm (an error from another realm, a thrown string or a plain object) is recorded like any other error: an exception event, `error.type`, and a status description that is the message only. Before, the status was `String(error)` (`"TypeError: msg"` or `"[object Object]"`) with no exception event.
- Depends on `@traceai/fi-core` 1.1.0, which masks the attributes passed at span creation and adds `hideErrorDetails`. 0.1.0 was published against `@traceai/fi-core` `^0.1.16`.

## [0.1.0]
- Initial release.
