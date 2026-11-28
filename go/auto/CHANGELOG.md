# go/auto changelog

Release history of `github.com/future-agi/traceAI/go/auto`. Each version is published as the tag `go/auto/v<version>`.

## Unreleased

### Added

- `auto.Attach(name)` lets a dedicated module's blank-import package send its spans through the agent's route, at the application's global SDK provider or the agent's private one. The module is reported as `auto` instead of `manual` while the agent is active or armed, follows `FI_INSTRUMENTATIONS` and `OTEL_SDK_DISABLED` without activating the agent when it is not allowed, applies the wire rules (structural capture, hidden image references, every `FI_HIDE_*` switch) to the span name, attributes, events, links and status of the copy the application's own provider exports. Known credentials are replaced in that copy as the span ends (its events and links are added then, so links given at span start are added when the span ends and a sampler does not see them at start) and, in the copy sent to Future AGI, at export. A value longer than the provider's attribute value-length limit is replaced whole and then cut, on both copies, so a secret learned after the write cannot leave a fragment at the cut. A recorded stack trace is cut to 2 048 bytes after the replacement. The copy sent to Future AGI keeps exactly the events and links the provider's limits keep (the oldest are dropped past the count limits, a link the SDK refuses is not counted, and what the SDK keeps of an event's or a link's attributes, the first ones up to the per-event and per-link limit, is what is sent; a link the SDK refuses after the privacy switches have removed its attributes is not counted either), and the record held while the span is open is cut by the same count limits and attribute limits as it grows. Events and links added while the span is ending are ignored on both copies. This covers what a module writes through the span it reads back from the context, a rename and a late link. The module never replaces `http.DefaultTransport`, and can wait for its background spans for up to half of the shutdown budget. `go/eino/auto` uses it. No dependency was added.
- The `FI_HIDE_*` rules cover a retriever's query (`gen_ai.retrieval.query`, with the input switches) and retrieved document content and metadata (`retrieval.documents.N.document.content` and `.document.metadata*`, with the output switches); they are replaced by `__REDACTED__`.

### Changed

- Credential value shapes are scanned in time linear in the text, and a token is replaced whole whatever its length.
- FI API keys are sent only over a checked transport. Literal loopback (`localhost`, `127.0.0.1`, `::1`) may use plaintext; Future AGI hosts (`futureagi.com` and subdomains, one trailing dot and case ignored) require `https://`, and no setting relaxes that. Any other `http://` `FI_BASE_URL` is refused unless `FI_ALLOW_INSECURE_HTTP` is exactly `1`, which logs one value-free warning per process. Other spellings of loopback (`127.1`, `2130706433`, `127.0.0.1.`) are not literal loopback. A written host that differs from the parsed host, an empty host, or a loopback name that connects to a non-loopback peer, is refused. A request that carries the keys in plaintext is sent directly, never through a proxy; an `https://` request uses the configured proxy (`HTTPS_PROXY`, `NO_PROXY`) through a CONNECT tunnel, with the certificate verified against the written host. A 3xx answer to an export is never followed: the batch is dropped, the export call returns no error, and the agent logs one value-free warning per process (`instrumentation unavailable`). `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` that name an `X-Api-Key` or `X-Secret-Key` header (any case, percent-decoded, in any comma-separated pair) or carry a Future AGI key value (`FI_API_KEY`, `FI_SECRET_KEY` or the keys given to `Init`) are refused. `Init` returns a value-free error, and blank-import arming is refused with one warning (`instrumentation unavailable`) and reports the wire integration as failed. The refusal applies only when the agent builds its own Future AGI exporter: not with `WithSpanProcessor`, and not without credentials. A key value is matched as written and percent-decoded, in the whole variable and in each pair on its own, from 8 characters; other headers, including another vendor's key, are left alone. An IPv6 zone literal such as `https://[fe80::1%25en0]:4318` is read as the same host the URL parser reads, once at `Init` and once on every export, and is never loopback. Refusals never print the URL, a peer address or the keys.

## [0.2.1](https://github.com/future-agi/traceAI/compare/go/auto/v0.2.0...go/auto/v0.2.1) (2026-11-28)


### Features

* **go/auto:** one decision function for FI key transport: TLS for FI hosts, plaintext opt-in, no proxies, no redirects ([f0ef042](https://github.com/future-agi/traceAI/commit/f0ef042d64c069f4f92082eb35e1ae7c6961c78c))
* **go:** Eino blank import — wire-equal masking, per-run dedup, bounded activation and shutdown, release gate ([d0e882c](https://github.com/future-agi/traceAI/commit/d0e882cba848a1dd49158362c1448c475b823899))
* **go:** send Future AGI keys only over a checked transport ([4b65ea3](https://github.com/future-agi/traceAI/commit/4b65ea3f8829cc9ea65558ecdb9efdf4aaa2150d))
* **go:** trace Eino from one blank import ([9cf127a](https://github.com/future-agi/traceAI/commit/9cf127a64ca98415ba00a87c78191eb0defd364f))
* **go:** trace Eino from one blank import through the agent's route ([711e8a9](https://github.com/future-agi/traceAI/commit/711e8a9720ffd2ed627b59561f55b5a7da364ae0))


### Bug Fixes

* **go/auto:** ambient OTLP headers carrying keys are refused, the dial-guard refusal names no peer, zone literals are read as written, test hooks are race-safe ([1531140](https://github.com/future-agi/traceAI/commit/15311408dd2a1b9c7a8a054a86925d21ef0214e3))
* **go/auto:** End replays and trims one snapshot; a link is refused after masking; restored event and link attributes are the SDK's first N ([8a875d8](https://github.com/future-agi/traceAI/commit/8a875d8ff9000987ee83e49efcfa251e86f5b4b6))
* **go/auto:** events and links of the Future AGI copy are the ones the SDK kept; a value over the length limit is replaced whole before the cut; the record is bounded ([94c67e6](https://github.com/future-agi/traceAI/commit/94c67e607c0e7de36804542d9964008d36f349f4))
* **go/auto:** events, links and status of a module span are kept raw and replaced with the end-time and export-time snapshots; replace, then cap; one stack trace capture ([fa6ebdd](https://github.com/future-agi/traceAI/commit/fa6ebddd9f15ae7b5f05fec8eece81a03e8f7151))
* **go/auto:** proxies off only for plaintext key sends; https keeps the environment proxy ([85024ba](https://github.com/future-agi/traceAI/commit/85024baff4d330dc6e903f2cb339bfe2892a8077))
* **go/auto:** retriever content follows the content switches; known secrets are replaced in the application's copy at the end of the span and in Future AGI's at export; RecordError like the SDK ([b7c4328](https://github.com/future-agi/traceAI/commit/b7c4328080f3186db59611d8905910f5d7c46825))
* **go:** keep the pending Eino module out of the release config; app copy follows the wire rules; lock-free module route; hooks outside the shutdown Once ([d71301b](https://github.com/future-agi/traceAI/commit/d71301bab536ffdaef4f179834eab017e76e8d4a))
* **privacy:** credential value shapes scan in linear time in every SDK, tokens replaced whole ([607ee0c](https://github.com/future-agi/traceAI/commit/607ee0c7658118db01601772a9d233bec59bbcbc))
* **privacy:** credential value shapes scan in linear time in every SDK, tokens replaced whole ([5defddd](https://github.com/future-agi/traceAI/commit/5defddd0dd3531d2e76d47c34075c14875ab8c22))

## [0.2.0] - 2026-11-18

### Added

- On agent-owned providers, sampled spans carry the trace's keep probability as the double attribute `fi.sampling.ratio` when it is strictly between 0 and 1, and a local fractional ratio decision writes `fi=r:<ratio>` to W3C tracestate (#713). Parent-based samplers copy a valid ratio from a sampled local or remote parent's `fi` entry at any root rate. An absent attribute means `1.0`. Roots decided at `0` or `1`, dropped or record-only spans and application-owned providers get no ratio; an invalid or absent parent starts a root without carrying its tracestate forward. Wire admission reserves one attribute slot for the ratio, and export privacy recovery keeps a valid ratio without restoring hidden content.

### Fixed

- Go auto instrumentation honors `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` when it creates a provider without an explicit sample rate (#711). Explicit FI rates keep parent-based ratio sampling; application-owned samplers remain unchanged, and invalid OTEL sampler settings fall back to the default with one warning.

## [0.1.0] - 2026-11-17

### Added

- First release: zero-code tracing of AI calls for Go applications (#707).
