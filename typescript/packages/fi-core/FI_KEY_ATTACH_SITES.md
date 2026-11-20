# Future AGI key attach sites

Inventory of every site in `fi-core/src` and `traceai_auto/src` (tests and `generated/` excluded) that reads,
sets or forwards `x-api-key` / `x-secret-key`, or hands a caller-supplied headers / metadata container to a
transport. Taken at the base of this change (before the single decider is introduced), then updated to the
sites that remain after it. Greps used (run from `typescript/packages`):

```
grep -rnEi 'x-api-key|x-secret-key|FI_API_KEY|FI_SECRET_KEY' fi-core/src traceai_auto/src --include=*.ts --exclude-dir=__tests__ --exclude-dir=generated
grep -rnE 'copyMetadata|[mM]etadata\.(set|add)\(|new (grpc\.)?Metadata|super\(\{|\bheaders\b *[:=(]|fetch\(|http\.request|https\.request|\.setHeader|\.append\(' fi-core/src traceai_auto/src --include=*.ts --exclude-dir=__tests__ --exclude-dir=generated
```

The second grep also matches unrelated code (outgoing W3C propagation in `traceai_auto/src/outgoing-*.ts`,
`wire/*.ts` observing the application's own requests, `attribute-projection.ts`): those read or add trace
headers on the application's requests and never carry FI keys; they are listed under "not attach sites".

## Attach sites (before)

| # | Site | What it does |
|---|------|--------------|
| 1 | `otel.ts::getEnvFiAuthHeader` | reads `FI_API_KEY` / `FI_SECRET_KEY` from the environment |
| 2 | `otel.ts::fiEnvKeysFor` | obtains the environment's keys, guard for the endpoint |
| 3 | `otel.ts::resolveFiHeaders` | merges explicit headers with environment keys |
| 4 | `otel.ts::headersForEndpoint` | final HTTP header decision for an exporter |
| 5 | `otel.ts::grpcChannelCredentials` | gRPC TLS / plaintext decision |
| 6 | `otel.ts::GRPCSpanExporter.constructor` | builds the outgoing metadata (`copyMetadata` + `metadata.set`) |
| 7 | `otel.ts::copyMetadata` | forwards every caller metadata value |
| 8 | `otel.ts::OTLPHTTPSpanExporter.constructor` | `super({ url, headers })` |
| 9 | `otel.ts::HTTPSpanExporter.constructor/export` | `fetch(..., { headers })` |
| 10 | `otel.ts::FITracerProvider.constructor` | resolves headers, builds the default exporter |
| 11 | `otel.ts::register` | passes headers / `allowInsecureHttp` to the provider |
| 12 | `otel.ts::checkCustomEvalConfigExists` | `fetch` with env keys |
| 13 | `otel.ts::checkCustomEvalTemplateExists` | `fetch` with env keys |
| 14 | `otlp-header-isolation.ts::isolateHttpExporterHeaders` | rebuilds the HTTP delegate's header provider |
| 15 | `otlp-header-isolation.ts::isolateGrpcExporterHeaders` | rebuilds the gRPC delegate's metadata |
| 16 | `fi-route.ts::createFiRouteSpanExporter` / `snapshotHeaders` | route factory for @traceai/auto |
| 17 | `fi-key-pair.ts::hasCompleteKeyPair` | names only |
| 18 | `traceai_auto/src/index.ts` (`init`) | passes `{ "x-api-key", "x-secret-key" }` to the route factory |
| 19 | `traceai_auto/src/config.ts::resolveConfig` | reads `env.FI_API_KEY` / `env.FI_SECRET_KEY` as one credential source |

## Not attach sites

`privacy-runtime.ts::credentialSecrets` (discovers secret values for redaction; sends nothing),
`traceai_auto/src/outgoing-*.ts`, `incoming-parent.ts`, `wire/*` (trace propagation / observation on the
application's own requests).

## After this change

One function, `decideFiRequest` (`fi-request-policy.ts`), decides for every path: whether the destination is
Future AGI bound, whether TLS is used, and whether keys may be attached. It is the only code that reads the
FI key environment variables or writes key-named headers/metadata, and it returns the FINAL outgoing set
(HTTP headers, or the built gRPC `Metadata`). Sites 1-7 collapse into it; sites 8-15 take its result;
sites 16, 18 and 19 are pinned by `fiKeyAttachPoints.test.ts` (route factory, auto side). The test uses the
TypeScript compiler API to assert the exact set of functions that mention the key names.

## Checks that sit beside the decider

- `assertDirectFetch` (`fi-request-policy.ts`) runs right before each fetch that carries keys (the deprecated
  `HTTPSpanExporter.export`, `checkCustomEvalConfigExists`, `checkCustomEvalTemplateExists`): a plaintext request to a host
  that is not loopback is refused when the dispatcher in use (the caller's, else the global one, read at send time) would proxy it.
  Those fetches use `redirect: "manual"` and treat a 3xx answer as an error.
- `connectGrpcDirect` (`otlp-header-isolation.ts`) builds the gRPC channel of an exporter that sends keys in plaintext under the opt-in
  with `grpc.enable_http_proxy: 0`.
- `assertNoKeysInAmbientHeaders` (`fi-request-policy.ts`) runs in the constructors of the exporters for another collector, where
  the OTLP exporter reads `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_EXPORTER_OTLP_TRACES_HEADERS`: it refuses a key header name or a known
  secret in those variables. It reads the key values only to refuse them; it sends nothing.

## Send-level guard

The sites above assemble what is sent. `fi-send-guard.ts` judges what is actually sent: `guardSend` takes the final URL and the final
headers or metadata of one send and the set the decision allowed, and refuses a key header the decision did not add, an FI key value under
another name, a plaintext send the rules refuse, a plaintext send through a proxy, and (for `fetch`) a redirect answer. Its callers are
`guardedFetch` (the deprecated HTTP exporter and the two eval checks), `guardOtlpHttpExporter` (the OTLP HTTP exporter's transport send,
wrapped in the exporter's constructor) and `guardOtlpGrpcExporter` (the gRPC client, built with an interceptor, in the gRPC exporter's
constructor). `fiSendGuardPin.test.ts` pins that every transport primitive in these modules is inside those three functions.
