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

## Coverage by framework operation

E means the adapter reads the original typed media value. R means the integration only keeps references that another integration already issued. N means not supported.

| Framework | Operation | Disposition |
| --- | --- | --- |
| LangChain (`@langchain/core` 0.2 to 1.x) | Chat model input messages (`image_url` data URIs; `image`/`audio`/`file` parts with `source_type: "base64"`) | E |
| LangChain | Chat model output generations with the same typed parts | E |
| LangChain | Chain, tool and retriever inputs/outputs | N: arbitrary objects are not scanned |
| LlamaIndex (legacy package) | `chat` input messages and non-streaming chat response (`image_url` data URIs) | E |
| LlamaIndex | Streaming chat output | N |
| Vercel AI SDK 5, 6, 7 | `generateText`/`streamText`/`generateObject`/`streamObject` prompt messages (`image`, `file` parts) | E |
| Vercel AI SDK 7 | Model response `file` content and typed tool `content` results (`image-data`, `file-data`) | E |
| Vercel AI SDK 5, 6 | Model output and tool results | N: the native telemetry path exposes only serialized text |
| Genkit 1.42+ | `defineModel` implementation wrapped with `withGenkitModelAttachments` (request messages and response message) | E |
| Genkit | Any model, including plugin models, through `use: [genkitAttachmentMiddleware(binding)]` on `generate` | E |
| Genkit | `defineTool` implementation returning `Part[]`, wrapped with `withGenkitToolAttachments` | E |
| Mastra (`@traceai/mastra`) | All exporter operations | N for raw capture. The exporter sees spans only after they end, has no manager and never uploads. Newer runtimes keep inline images in span input, but the core has no API to add references to an ended span. The exporter removes `fi.attachment.*` keys it did not issue. |

Streams are never read for media, remote URLs and file paths are never fetched, and creating new recordings is not supported.

## Known limitation: framework plus provider instrumentation

When a framework integration and a provider integration (for example LangChain plus `@traceai/openai`) are both given the same manager, the same image is uploaded once for the framework span and once for the provider span. The current attachment core has no cross-span occurrence claim, so these adapters cannot deduplicate across span boundaries. Give the manager to one layer only until that core API exists.
