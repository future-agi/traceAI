# Framework attachment adapters

Shared typed-media normalization for traceAI's framework integrations. This package is an adapter dependency, not a separate uploader or recorder.

## Capture boundary

`captureMediaMessages(binding, span, messages, dialect, direction)` observes typed message content. `captureMediaParts(binding, span, parts, dialect, options)` observes a known typed part array. Supported dialects are `langchain`, `llamaindex`, `vercel`, and `genkit`.

Pass the existing provider's `attachments` manager in `binding`. Without that manager, these functions do nothing. The manager still requires explicit attachment enablement, a successful policy bootstrap and a sampled, recording span. Normal inference never waits for attachment network operations. Use the provider's existing flush/shutdown lifecycle to finish admitted transfers.

The adapters accept inline base64/data URIs and `Uint8Array` sources at the documented typed boundaries. They do not fetch remote URLs, open local paths, read streams, reconstruct discarded media from exported text, or create new recordings. Base64 decoding is bounded to one MiB per part. A single span admits at most 64 positions through this helper; the core manager separately bounds bytes, queue length and reference projection.

## Privacy and identity

- Capture is off unless enabled on the shared manager.
- Directional hide flags, required PII handling, server policy and native content restrictions deny capture before decoding or copying media.
- Supply the integration's `traceConfig` and `capture` restrictions along with the manager. Binding-level denials cannot be relaxed by an individual part.
- Tool images intended for a model are subject to both output and model-input restrictions.
- No filenames, source URLs, hashes or provider credentials are added to references.
- Repeated observation of the same admitted message/part position on the same span is idempotent. Different message positions remain distinct occurrences. This is not cross-provider deduplication or permission to relink an occurrence to an ancestor span.
- The shared manager owns all transport, write capabilities and occurrence correlation. An upload completing into validation is not proof of authorized playback.

`mediaField` reads own data descriptors only. The traversal is limited to known message/part fields; application getters and `toJSON` methods are not invoked.
