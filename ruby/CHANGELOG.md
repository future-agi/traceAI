# Changelog

## [0.1.1](https://github.com/future-agi/traceAI/compare/ruby-traceai-auto-v0.1.0...ruby-traceai-auto-v0.1.1) (2026-11-28)


### Features

* **ruby:** export through an HTTPS proxy with CONNECT and full TLS verification ([0649a91](https://github.com/future-agi/traceAI/commit/0649a91a983f3273637be33587f20e29174beed6))
* **ruby:** key transport: one trailing dot, case-insensitive loopback, one warning under threads, opt-in fixed at construction, one refusal warning ([d85fda1](https://github.com/future-agi/traceAI/commit/d85fda1db494c2df038ef8b794bd9d3040a11dab))
* **ruby:** one gate for FI key transport: TLS for FI hosts, plaintext opt-in, loopback peer check, redirects refused ([92afb78](https://github.com/future-agi/traceAI/commit/92afb7803a64864b68fda51bfb062b92dc99cf75))
* **ruby:** one gate for FI key transport: TLS for FI hosts, plaintext opt-in, loopback peer check, redirects refused ([f215769](https://github.com/future-agi/traceAI/commit/f21576987d0829378b7270f5ed75e11bb7c2ede9))
* **ruby:** RubyLLM chat and tool spans from the instrumentation events, zero-code ([66ab70b](https://github.com/future-agi/traceAI/commit/66ab70b0fe98670bf7a2e9fecc43dbfe66284da0))
* **ruby:** RubyLLM chat and tool spans, zero-code ([bab165a](https://github.com/future-agi/traceAI/commit/bab165a596340b1261cca158a4e231c416f0b058))
* **ruby:** send Future AGI keys only over a checked transport ([844abc7](https://github.com/future-agi/traceAI/commit/844abc7b044af3e7946e8527569034cef3ecbeeb))


### Bug Fixes

* **privacy:** credential value shapes scan in linear time in every SDK, tokens replaced whole ([607ee0c](https://github.com/future-agi/traceAI/commit/607ee0c7658118db01601772a9d233bec59bbcbc))
* **privacy:** credential value shapes scan in linear time in every SDK, tokens replaced whole ([5defddd](https://github.com/future-agi/traceAI/commit/5defddd0dd3531d2e76d47c34075c14875ab8c22))
* **ruby:** an unrunnable media probe fails the suite instead of skipping; CI expects proven hooks on a supported SDK; the warning says content, events and links are not exported ([aeb46b4](https://github.com/future-agi/traceAI/commit/aeb46b424ea80f5f5220ff45b8ff724952852993))
* **ruby:** bound one span's scrub work, fail closed, keep hide settings and identity past the bound ([df0210d](https://github.com/future-agi/traceAI/commit/df0210d61a549c5cfcfab1eaa510c43db8e6bda5))
* **ruby:** bound the overlapping-secrets matcher's work per value ([64b8c80](https://github.com/future-agi/traceAI/commit/64b8c80b6c3b978e3ecabc45fb18810f855843a8))
* **ruby:** declare the OpenTelemetry SDK floor the span snapshot needs (1.10) and an exporter floor that resolves with it ([982795c](https://github.com/future-agi/traceAI/commit/982795cadd33a16806932244640a3d8bf977745f))
* **ruby:** export with OpenTelemetry SDK 1.3 to 1.9 and verify span hooks at startup ([fdebc1d](https://github.com/future-agi/traceAI/commit/fdebc1d7daa2dc6ed41413561194797684a13092))
* **ruby:** forward spans from OpenTelemetry SDKs whose span data has no parent_span_is_remote ([e6126e0](https://github.com/future-agi/traceAI/commit/e6126e0d70dfb232a14de3f953e5bcf197676c83))
* **ruby:** omit media when the SDK span hooks cannot be verified; copy events and links of any shape ([33cd218](https://github.com/future-agi/traceAI/commit/33cd2181d5aad17b8ec3ca155b7ab327372f4d2c))
* **ruby:** overlapping known secrets are replaced as a whole on every copy ([de3a37a](https://github.com/future-agi/traceAI/commit/de3a37a4214b0a31e626d27a003e521f3d60fa53))
* **ruby:** secrets that overlap are replaced as a whole through derived variants ([df67fdb](https://github.com/future-agi/traceAI/commit/df67fdb86d5bbb09ec63b98dcca444c251d713bf))
* **ruby:** span bound follow-ups — media context from original attributes, one normalisation per span, capped secret runs ([0650417](https://github.com/future-agi/traceAI/commit/0650417cf7b96dfa80b7b69fd617080dbed98bac))

## Changelog

All notable changes to `traceai-auto` are documented here. Release notes are added
by the release process.

## Unreleased

### Added
- RubyLLM 2.x (`>= 2.0, < 3`) is instrumented with no application code: each model request has a `chat <model>` span (streamed or not) and each tool call an `execute_tool <name>` span, with GenAI model, usage, finish-reason and message attributes, tool arguments and results, and errors recorded and re-raised. The HTTP request inside a chat span is not reported a second time. `FI_DISABLED_INSTRUMENTATIONS=ruby_llm` turns it off, and the doctor reports `ruby_llm` as `instrumented` when a supported RubyLLM is installed.
- Raw input and raw output (`raw.input`, `raw.output` and their children) are not recorded while any content hide setting is on, and a tool call's name and id stay visible while its arguments and result follow the input and output hide settings.

### Changed
- Within one span, the work of scrubbing known secrets that overlap is bounded as a whole; every later value of the span, numbers and booleans included, and every later attribute name is replaced with the placeholder (a base64 media string keeps its `<omitted: N base64 chars>` text, and the last value written under the shared placeholder name is the one exported), the hide settings still remove what they name (by the name the attribute came with), and the span keeps its name, status text, resource and scope. A secret you pass to `scrub`, `scrub_provider_response` or `clean_string` with invalid bytes is read with each invalid byte as U+FFFD (as the registry reads one) instead of being skipped, and its 8 longest runs of 8 or more valid characters are secrets of their own.
- A media payload beside a mime type is omitted whether or not a hide setting hides that mime type: the media context of a span's attributes, of each event's and of each link's is read from the attributes as they came, before a hide setting replaces any value.
- A span reads and sorts the secrets it scrubs with once, for its name, values, events and links together. When a span carries two attribute names that read the same (`gen_ai.operation.name` as a String and as a Symbol), the last one is the attribute the span exports and the operation name that decides how a transcript is hidden.
- A secret passed to `register_secret` that has invalid bytes (a byte slice that cuts through a multi-byte character) also registers up to 8 of its longest runs of 8 or more valid characters, as a secret passed to `scrub` does. After an error in the span's own scrub, the fallback applies the hide settings by the names the attributes came with, as the main path does, before a registered secret changes any name.
- A value with invalid bytes protects up to 8 of its longest runs of 8 or more valid characters, whether it is registered or passed to `scrub`; one registration takes at most 9 places in the registry, so it cannot push out the Future AGI keys or the environment secrets by itself.
- Known secrets that overlap one another, in any number and at any overlap (a shared edge of any length, a secret inside another, chains of three or more), are replaced as a whole in the copy sent to Future AGI at export and in a span captured after they were known; a value that would need more matching work than the bounded limit is replaced entirely. Values registered as secret after a span has ended are replaced in the copy sent to Future AGI; the application's own exporters receive spans as they were when they ended. A RubyLLM span that is still open when a secret is learned has its values replaced again when it ends, so both copies are clean.
- Future AGI keys are sent only over a checked transport. Literal loopback (`localhost`, `127.0.0.1`, `::1`) may use plaintext; Future AGI hosts (`futureagi.com` and subdomains, case and one trailing dot ignored) require `https://` and no setting relaxes that. Any other `http://` `FI_BASE_URL` is refused unless `FI_ALLOW_INSECURE_HTTP` is exactly `1`, which logs one warning per process. Other spellings of loopback (`127.1`, `2130706433`, `127.0.0.1.`) are not literal loopback. A URL with userinfo, a backslash, `%` or `@` anywhere is refused, as is a loopback name that connects to a non-loopback peer. A 3xx answer to an export is a refusal. Refusals never print the URL or the keys.
- The first Future AGI export refused by the key transport check logs one warning per process. It names a reason from a short list (insecure target, redirect, peer not loopback, unparseable URL) and never the URL or a key. `FI_ALLOW_INSECURE_HTTP` is read once, when the exporter starts. A plaintext send, loopback included, always connects directly to its target and ignores every proxy setting.
- Export works behind a corporate HTTPS proxy. An `https://` target is sent through the proxy named by `https_proxy` or `HTTPS_PROXY` (the lower-case name first) with a CONNECT request, unless `no_proxy` or `NO_PROXY` matches the host (read as Ruby reads it: `host` matches the host and its subdomains, `.domain` only subdomains, `host:port` limits the port, and an IPv4 address or CIDR such as `10.0.0.0/8` matches an IPv4 target; `*` is not special; a name is never resolved to test an entry) or the host is loopback; those go direct. The proxy URL must be `http://` or `https://`, optionally with `user:password@`, which is sent to the proxy only, as `Proxy-Authorization: Basic`. Any other scheme, or a value that cannot be read (including bytes that are not text in a proxy variable), refuses the export with one warning that names a reason and never the setting, and the agent keeps running. A proxy that cannot be reached, closes, answers too much or too slowly, or a tunnelled session that cannot be verified, gives the same one warning. An `https://` proxy has its own certificate and name verified before anything is sent to it. The tunnelled session is always verified in full against the Future AGI host, never the proxy: a certificate that does not chain to a trusted authority or does not match the host is refused before any key is written. A proxy that refuses the tunnel, answers with a redirect or fails is a refused export; it never turns into a direct send. The settings are read once, when the exporter starts, and the proxy URL is never printed.
- Forwarding through the application's OpenTelemetry SDK is tested on `opentelemetry-sdk` 1.10 and later (the lock pins 1.10.0) with the locked `opentelemetry-api` 1.8 (CI runs the lock; API 1.11 is not part of the CI run). Spans from SDK 1.3 through 1.9 are forwarded too, with a local parent because their span data does not report whether the parent is remote.
- At startup the agent runs a one-time probe of its media bookkeeping hooks on a throwaway span of the SDK in use. When the probe cannot verify them, the spans the agent exports (AI and server spans, and every span sent to an exporter you pass to the agent) are still forwarded but keep only identity, timing and usage metadata (content, events and links are not exported) and one line says so; SDK 1.3 and 1.4 are such SDKs.
- Credential value shapes are scanned in time linear in the text, and a token is replaced whole whatever its length.

### Fixed
- Span events and links are copied whether the SDK and API define them as structs or as plain objects, with the same masking.
- Forwarding to an application SDK exporter keeps a batch when the SDK's span data has a member the agent does not know; that member is left empty.
