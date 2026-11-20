# Changelog

All notable changes to `traceai-auto` are documented here. Release notes are added
by the release process.

## Unreleased

### Added
- RubyLLM 2.x (`>= 2.0, < 3`) is instrumented with no application code: each model request has a `chat <model>` span (streamed or not) and each tool call an `execute_tool <name>` span, with GenAI model, usage, finish-reason and message attributes, tool arguments and results, and errors recorded and re-raised. The HTTP request inside a chat span is not reported a second time. `FI_DISABLED_INSTRUMENTATIONS=ruby_llm` turns it off, and the doctor reports `ruby_llm` as `instrumented` when a supported RubyLLM is installed.
- Raw input and raw output (`raw.input`, `raw.output` and their children) are not recorded while any content hide setting is on, and a tool call's name and id stay visible while its arguments and result follow the input and output hide settings.

### Changed
- Forwarding through the application's OpenTelemetry SDK is tested on `opentelemetry-sdk` 1.10 and later (the lock pins 1.10.0) with the locked `opentelemetry-api` 1.8 (CI runs the lock; API 1.11 is not part of the CI run). Spans from SDK 1.3 through 1.9 are forwarded too, with a local parent because their span data does not report whether the parent is remote.
- At startup the agent runs a one-time probe of its media bookkeeping hooks on a throwaway span of the SDK in use. When the probe cannot verify them, the spans the agent exports (AI and server spans, and every span sent to an exporter you pass to the agent) are still forwarded but keep only identity, timing and usage metadata (content, events and links are not exported) and one line says so; SDK 1.3 and 1.4 are such SDKs.

### Fixed
- Span events and links are copied whether the SDK and API define them as structs or as plain objects, with the same masking.
- Forwarding to an application SDK exporter no longer drops a batch when the SDK's span data has a member the agent does not know; that member is left empty.
