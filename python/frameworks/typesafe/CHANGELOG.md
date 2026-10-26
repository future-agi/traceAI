# Changelog

## [0.1.0] - 2026-10-16
### Feature
- First release. `TypeSafeInstrumentor` traces `typesafe_sdk.TypeSafeClient.system_one` and
  `AsyncTypeSafeClient.system_one` (typesafe-sdk 0.7.0 to < 1; tested on 0.7.0, 0.7.4 and 0.7.5):
  one `EVALUATOR` span per decision call with `typesafe.question.<name>.*` attributes for
  `noul`, `choice`, `score` and `box` answers, request and response model, token usage and the
  request id. Content follows the traceAI `TraceConfig` convention (captured by default,
  `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` turn it off). `client.models.list()` is not traced.
