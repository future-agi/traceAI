

## [0.1.12] - pending release

### Changed
- traceai-litellm now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- LiteLLM spans record only known invocation parameters on completion, embedding and image generation calls, sync and async, so API keys and headers passed to LiteLLM are no longer recorded in span attributes.
- A failed LiteLLM stream ends its span and restores the current context, async stream setup keeps the provider's exception, and successful streams record token usage when the stream includes it.
- Completion calls with a structured-output response_format class, or another argument that tracing cannot serialize, complete normally.
- Positional calls record their model and input, image-generation calls record token usage when LiteLLM reports it, and input capture keeps a conversation's system and earlier text messages.

## [0.1.11] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.10 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.8] - 2025-07-08
### Feature
- Added support for OpenAI's Dall-E image generation models

## [0.1.7] - 2025-06-10
### Feature
- Added support for ai-evaluation

## [0.1.6] - 2025-05-29
### Feature
- Updated dependencies to the latest versions.

## [0.1.5] - 2025-05-23
### Feature
- Added support for FutureAGI's protect

## [0.1.4] - 2025-05-08
### Feature
- Updated dependencies to the latest versions.

## [0.1.3] - 2025-04-14
### Changed
- Updated dependencies to the latest versions.
