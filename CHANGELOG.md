# Changelog

All notable changes to traceAI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note**: This is an aggregated changelog. For detailed package-specific changes, see the CHANGELOG.md in each package directory:
> - Python Core: [`python/CHANGELOG.md`](python/CHANGELOG.md)
> - Python Frameworks: [`python/frameworks/{framework}/CHANGELOG.md`](python/frameworks/)
> - TypeScript Core: [`typescript/packages/fi-core/CHANGELOG.md`](typescript/packages/fi-core/CHANGELOG.md)
> - TypeScript Packages: [`typescript/packages/{package}/CHANGELOG.md`](typescript/packages/)

---

## [Unreleased]

### Fixed
- Java Anthropic instrumentation unwraps optional temperature values, so requests with an absent or configured temperature reach the client instead of failing during tracing.

### Coming Soon
- Additional framework integrations
- Enhanced streaming support
- Performance optimizations

### Java privacy controls
- `hideInputs` and `hideOutputs` now also suppress their corresponding messages recorded by `FITracer`, even when the separate message flags are false.
- Add opt-in `hideErrorDetails` / `FI_HIDE_ERROR_DETAILS` (default off) to omit error messages, exception stack traces and ERROR status descriptions while retaining error types and a sanitized exception event.
- Cohere tracing honors input/output privacy controls for preambles, rerank queries and generated tool calls, and uses the core error-detail policy for chat, embed and rerank failures.

---

## [2026-10-18]

### TypeScript

> **Security fix (all published `@traceai/fi-core` releases, 0.1.0 to 1.0.0):** content hiding did not apply to the attributes an instrumentation passes when it creates a span, and most instrumentations put the prompt there. With `FI_HIDE_INPUTS=true`, `@traceai/openai` still exported the prompt in `input.value`, `gen_ai.input.messages` and `raw.input`. Fixed in `@traceai/fi-core` 1.1.0. Upgrade it, and upgrade `@traceai/openai`, `@traceai/anthropic`, `@traceai/bedrock` and `@traceai/langchain` to the releases below: their published versions depend on `@traceai/fi-core` `^0.1.x` and keep the bug.

#### @traceai/fi-core `v1.1.0`
- 🔒 Attributes passed to `FITracer.startSpan` / `startActiveSpan` go through the same masking as `setAttribute(s)` (hide flags, PII redaction, error-detail hiding). They were exported unmasked. Samplers still see them, masked.
- 🔒 `hideInputs` also redacts `raw.input` and `llm.prompt_template.variables`; `hideOutputs` also redacts `raw.output`.
- ✨ New `hideErrorDetails` trace config option (`FI_HIDE_ERROR_DETAILS`). Unset, it turns on with any content-hiding flag (`hideInputs`, `hideOutputs`, `hideInputMessages`, `hideOutputMessages`, `hideInputText`, `hideOutputText`); explicit `true` / `false` wins. When on, ERROR statuses have no description, and `exception.message`, `exception.stacktrace`, `error.message` and keys ending in `.exception.message` / `.exception.stacktrace` / `.error.message` / `.error_message` are dropped from spans and events. Other attributes, `exception.type` included, are kept.
- ✨ Error spans carry `error.type` (provider error code, else error class). `exception.type` is the same in both modes and never a numeric code.
- 🐛 Status descriptions are the error message only, never a stack trace. Multi-line messages are kept whole, and a relayed Python traceback keeps its final `Type: message` line.
- 🐛 `FI_HIDE_*`, `FI_PII_REDACTION` and `FI_BASE64_IMAGE_MAX_LENGTH` env vars apply even when no `traceConfig` is passed.
- ⚠️ Behaviour change: users of the hide flags now also get error details hidden, and creation-time content redacted.
- ⚠️ Behaviour change: inside `FITracer.startActiveSpan`, `trace.getActiveSpan()` is the `FISpan`, not the SDK span. Code that casts it to the SDK `Span` / `ReadableSpan` breaks there.

#### @traceai/openai `v0.1.14`
- 🔒 Depends on `@traceai/fi-core` 1.1.0, so the prompt passed at span creation is hidden by the hide flags.

#### @traceai/anthropic `v0.1.3`
- 🔒 Depends on `@traceai/fi-core` 1.1.0, so the prompt passed at span creation is hidden by the hide flags.

#### @traceai/bedrock `v0.1.1`
- 🐛 Non-`Error` failures (cross-realm errors, thrown strings, plain objects) get an exception event, `error.type` and a message-only description instead of `String(error)`.
- 🔒 Depends on `@traceai/fi-core` 1.1.0.

#### @traceai/langchain `v0.1.3`
- 🐛 Status description is the error message, not LangChain's `run.error` (message plus full stack trace).
- 🔒 Depends on `@traceai/fi-core` 1.1.0 (prompt template variables are hidden with `hideInputs`).

#### @traceai/openai-agents `v0.2.1`
- 🐛 Status description is the error message only; error `data` moves to the exception event, whose type is `AgentSpanError`.

---

## [2025-07-08]

### Python

#### traceAI-openai `v0.1.8`
- ✨ Introduced compatibility with OpenAI's latest response method
- ✨ Enabled functionality for OpenAI's latest multi-modal feature

---

## [2025-06-10]

### Python

#### fi-instrumentation-otel (Core) `v0.1.7`
- ✨ Added support for ai-evaluation
- ✨ Added support for new evals in Prototype

#### traceAI-openai `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-anthropic `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-langchain `v0.1.9`
- ✨ Added support for ai-evaluation

#### traceAI-llamaindex `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-mistralai `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-vertexai `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-crewai `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-haystack `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-litellm `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-groq `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-autogen `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-guardrails `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-smolagents `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-dspy `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-bedrock `v0.1.7`
- ✨ Added support for ai-evaluation

#### traceAI-instructor `v0.1.7`
- ✨ Added support for ai-evaluation

### TypeScript

#### @traceai/fi-core `v0.1.10`
- ⬆️ Updated OpenTelemetry to v2.0.1
- ⬆️ Updated dependencies
- 🐛 Bug Fixes

#### @traceai/openai `v0.1.10`
- ⬆️ Dependencies Updated

#### @traceai/anthropic `v0.1.1`
- ⬆️ Dependencies Updated

---

## [2025-06-05]

### Python

#### traceAI-langchain `v0.1.8`
- 🐛 Bug Fixes
- ✨ Support for adding custom attributes through metadata

---

## [2025-06-03]

### Python

#### fi-instrumentation-otel (Core) `v0.1.6`
- ✨ Added Support for Custom Evaluations in Prototype

---

## [2025-05-29]

### Python

#### traceAI-openai `v0.1.6`
- ⬆️ Updated dependencies to the latest versions

#### traceAI-anthropic `v0.1.6`
- ⬆️ Updated dependencies to the latest versions

#### traceAI-langchain `v0.1.7`
- ⬆️ Updated dependencies to the latest versions

#### traceAI-mistralai `v0.1.6`
- ⬆️ Updated dependencies to the latest versions

#### traceAI-vertexai `v0.1.6`
- ⬆️ Updated dependencies to the latest versions

---

## [2025-05-23]

### Python

#### fi-instrumentation-otel (Core) `v0.1.5`
- ✨ Added support for FutureAGI's protect
- 🐛 Handling for incomplete spans during termination

#### traceAI-openai `v0.1.5`
- ✨ Added support for FutureAGI's protect

#### traceAI-anthropic `v0.1.5`
- ✨ Added support for FutureAGI's protect

#### traceAI-langchain `v0.1.6`
- ✨ Added support for FutureAGI's protect

#### traceAI-llamaindex `v0.1.5`
- ✨ Added support for FutureAGI's protect

#### traceAI-mistralai `v0.1.5`
- ✨ Added support for FutureAGI's protect

#### traceAI-vertexai `v0.1.5`
- ✨ Added support for FutureAGI's protect

---

## [2025-05-20]

### TypeScript

#### @traceai/fi-core `v0.1.1`
- ⬆️ Dependencies Updated

---

## [2025-05-19]

### TypeScript

#### @traceai/fi-core `v0.1.0`
- 🎉 Initial Release

---

## [2025-05-08]

### Python

#### fi-instrumentation-otel (Core) `v0.1.4`
- 🐛 Bug fixes
- ✨ Support for new evals in Prototype

#### traceAI-openai `v0.1.4`
- ⬆️ Updated dependencies to the latest versions

#### traceAI-langchain `v0.1.5`
- ⬆️ Updated dependencies to the latest versions

---

## [2025-05-02]

### Python

#### traceAI-langchain `v0.1.4`
- 🔧 Enhanced image data extraction and support for OpenAI CUA

---

## [2025-04-14]

### Python

#### fi-instrumentation-otel (Core) `v0.1.3`
- ✅ Validations for Prototype eval mapping
- ✨ Added Support for Audio Evaluations

#### traceAI-openai `v0.1.3`
- ⬆️ Updated dependencies to the latest versions
- ✨ Added Support for Audio and Image Generation models

#### traceAI-anthropic `v0.1.3`
- ⬆️ Updated dependencies to the latest versions

#### traceAI-langchain `v0.1.3`
- ⬆️ Updated dependencies to the latest versions
- 🔧 Enhanced attribute data extraction capabilities in LangChain, providing more efficient data handling

---

## Legend

- 🎉 Initial Release
- ✨ New Feature
- 🐛 Bug Fix
- 🔧 Enhancement
- ⬆️ Dependency Update
- 📝 Documentation
- ✅ Validation/Testing
- ⚠️ Breaking Change
- 🗑️ Deprecation

---

## Framework-Specific Changelogs

### Python Frameworks

| Framework | Changelog |
|-----------|-----------|
| Core | [python/CHANGELOG.md](python/CHANGELOG.md) |
| OpenAI | [python/frameworks/openai/CHANGELOG.md](python/frameworks/openai/CHANGELOG.md) |
| Anthropic | [python/frameworks/anthropic/CHANGELOG.md](python/frameworks/anthropic/CHANGELOG.md) |
| LangChain | [python/frameworks/langchain/CHANGELOG.md](python/frameworks/langchain/CHANGELOG.md) |
| LlamaIndex | [python/frameworks/llama_index/CHANGELOG.md](python/frameworks/llama_index/CHANGELOG.md) |
| MistralAI | [python/frameworks/mistralai/CHANGELOG.md](python/frameworks/mistralai/CHANGELOG.md) |
| VertexAI | [python/frameworks/vertexai/CHANGELOG.md](python/frameworks/vertexai/CHANGELOG.md) |
| CrewAI | [python/frameworks/crewai/CHANGELOG.md](python/frameworks/crewai/CHANGELOG.md) |
| Haystack | [python/frameworks/haystack/CHANGELOG.md](python/frameworks/haystack/CHANGELOG.md) |
| LiteLLM | [python/frameworks/litellm/CHANGELOG.md](python/frameworks/litellm/CHANGELOG.md) |
| Groq | [python/frameworks/groq/CHANGELOG.md](python/frameworks/groq/CHANGELOG.md) |
| Autogen | [python/frameworks/autogen/CHANGELOG.md](python/frameworks/autogen/CHANGELOG.md) |
| Guardrails | [python/frameworks/guardrails/CHANGELOG.md](python/frameworks/guardrails/CHANGELOG.md) |
| OpenAI Agents | [python/frameworks/openai-agents/CHANGELOG.md](python/frameworks/openai-agents/CHANGELOG.md) |
| SmolAgents | [python/frameworks/smolagents/CHANGELOG.md](python/frameworks/smolagents/CHANGELOG.md) |
| DSPy | [python/frameworks/dspy/CHANGELOG.md](python/frameworks/dspy/CHANGELOG.md) |
| Bedrock | [python/frameworks/bedrock/CHANGELOG.md](python/frameworks/bedrock/CHANGELOG.md) |
| Instructor | [python/frameworks/instructor/CHANGELOG.md](python/frameworks/instructor/CHANGELOG.md) |
| Google GenAI | [python/frameworks/google-genai/CHANGELOG.md](python/frameworks/google-genai/CHANGELOG.md) |
| Google ADK | [python/frameworks/google-adk/CHANGELOG.md](python/frameworks/google-adk/CHANGELOG.md) |
| Pipecat | [python/frameworks/pipecat/CHANGELOG.md](python/frameworks/pipecat/CHANGELOG.md) |
| Portkey | [python/frameworks/portkey/CHANGELOG.md](python/frameworks/portkey/CHANGELOG.md) |
| MCP | [python/frameworks/mcp/CHANGELOG.md](python/frameworks/mcp/CHANGELOG.md) |

### TypeScript Packages

| Package | Changelog |
|---------|-----------|
| Core | [typescript/packages/fi-core/CHANGELOG.md](typescript/packages/fi-core/CHANGELOG.md) |
| Semantic Conventions | [typescript/packages/fi-semantic-conventions/CHANGELOG.md](typescript/packages/fi-semantic-conventions/CHANGELOG.md) |
| OpenAI | [typescript/packages/traceai_openai/CHANGELOG.md](typescript/packages/traceai_openai/CHANGELOG.md) |
| Anthropic | [typescript/packages/traceai_anthropic/CHANGELOG.md](typescript/packages/traceai_anthropic/CHANGELOG.md) |
| LangChain | [typescript/packages/traceai_langchain/CHANGELOG.md](typescript/packages/traceai_langchain/CHANGELOG.md) |
| LlamaIndex | [typescript/packages/traceai_llamaindex/CHANGELOG.md](typescript/packages/traceai_llamaindex/CHANGELOG.md) |
| Bedrock | [typescript/packages/traceai_bedrock/CHANGELOG.md](typescript/packages/traceai_bedrock/CHANGELOG.md) |
| Vercel AI SDK | [typescript/packages/traceai_vercel/CHANGELOG.md](typescript/packages/traceai_vercel/CHANGELOG.md) |
| Mastra | [typescript/packages/traceai_mastra/CHANGELOG.md](typescript/packages/traceai_mastra/CHANGELOG.md) |
| MCP | [typescript/packages/traceai_mcp/CHANGELOG.md](typescript/packages/traceai_mcp/CHANGELOG.md) |

---

For questions about releases or to report issues, please visit our [GitHub Issues](https://github.com/future-agi/traceAI/issues).

