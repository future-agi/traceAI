## [0.1.2] - pending release
### Fix
- **Upgrade.** The standalone agent bundles the fi-core privacy runtime. 0.1.0 and 0.1.1 bundled fi-core 1.2.0 and 1.2.2, which could export short base64 media payloads that carry no recognized file signature: type- or modality-declared video (`type: video`, `type: input_video`, `modality: video`) at any `FI_BASE64_IMAGE_MAX_LENGTH`, including the default 0; type-declared images when the cap was positive; and images whose declaration is a separate attribute from the payload, at any cap. 0.1.2 is rebuilt against fi-core 1.2.3, which omits all of these at any cap. Setting the cap to 0 is not a workaround.
- The npm package now includes the Apache-2.0 `LICENSE` file.

### Known limitations
- As in fi-core 1.2.3: Image data nested inside a JSON-encoded string field (for example an image url inside a JSON string) may still be exported at any image cap, including the default. A structural fix is planned for a later release. Upgrade anyway: 0.1.0 and 0.1.1 (bundling fi-core 1.2.0 and 1.2.2) export this shape and more (typed video at every cap, flattened image parts at cap 0).

## [0.1.1] - 2026-11-08
- New README and documentation; the standalone agent bundles fi-core 1.2.2.

## [0.1.0] - 2026-11-07
- First release: zero-code Node auto-instrumentation (adapters off by default), with bundled third-party notices.
