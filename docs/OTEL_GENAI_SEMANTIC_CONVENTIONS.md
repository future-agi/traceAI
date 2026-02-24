# OpenTelemetry GenAI Semantic Conventions

## Overview

This document describes the OpenTelemetry (OTEL) GenAI Semantic Conventions that FutureAGI traceAI SDK implements. These conventions are the **industry standard** for LLM observability, originally developed by OpenLLMetry and now officially part of OpenTelemetry.

By following these standards, traceAI is compatible with any OTEL-compliant backend including:
- Datadog
- Langfuse
- LangSmith
- Dynatrace
- Jaeger
- Zipkin
- Any OpenTelemetry Collector

---

## Why OTEL GenAI Standards?

### Benefits

1. **Interoperability** - Traces work with any OTEL-compatible observability platform
2. **No Vendor Lock-in** - Users can switch backends without changing instrumentation
3. **Community Support** - Growing ecosystem of tools and integrations
4. **Future-Proof** - OTEL is the de facto standard for observability
5. **Consistent Vocabulary** - Same attribute names across all LLM providers

### Industry Adoption

- **Datadog** - Native support for OTEL GenAI conventions (v1.37+)
- **Langfuse** - Full OpenTelemetry integration
- **LangSmith** - OpenLLMetry semantic convention support
- **Dynatrace** - OpenLLMetry conventions support

---

## Attribute Reference

### Span Naming Convention

```
Span Name: {gen_ai.operation.name} {gen_ai.request.model}
Span Kind: CLIENT (or INTERNAL for local models)

Examples:
- "chat gpt-4o"
- "embeddings text-embedding-3-small"
- "text_completion claude-3-opus"
```

### Core Attributes

#### Provider & Operation

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.operation.name` | string | Yes | The operation being performed | `chat`, `text_completion`, `embeddings` |
| `gen_ai.provider.name` | string | Yes | The GenAI provider | `openai`, `anthropic`, `google`, `aws.bedrock` |
| `gen_ai.system` | string | No | Deprecated, use `gen_ai.provider.name` | `openai` |

#### Request Attributes

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.request.model` | string | Yes | Model being requested | `gpt-4o-mini`, `claude-3-opus` |
| `gen_ai.request.temperature` | double | No | Sampling temperature | `0.7` |
| `gen_ai.request.top_p` | double | No | Nucleus sampling parameter | `0.9` |
| `gen_ai.request.top_k` | int | No | Top-k sampling parameter | `40` |
| `gen_ai.request.max_tokens` | int | No | Maximum tokens to generate | `1024` |
| `gen_ai.request.frequency_penalty` | double | No | Frequency penalty | `0.5` |
| `gen_ai.request.presence_penalty` | double | No | Presence penalty | `0.5` |
| `gen_ai.request.stop_sequences` | string[] | No | Stop sequences | `["\n", "END"]` |
| `gen_ai.request.seed` | int | No | Random seed for reproducibility | `42` |

#### Response Attributes

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.response.model` | string | No | Actual model that responded | `gpt-4o-mini-2024-07-18` |
| `gen_ai.response.id` | string | No | Unique response identifier | `chatcmpl-abc123` |
| `gen_ai.response.finish_reasons` | string[] | No | Why generation stopped | `["stop"]`, `["length"]` |

#### Token Usage Attributes

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.usage.input_tokens` | int | Yes | Tokens in the prompt | `150` |
| `gen_ai.usage.output_tokens` | int | Yes | Tokens in the response | `50` |

#### Message Attributes

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `gen_ai.input.messages` | any (JSON) | No | Full chat history/prompt messages |
| `gen_ai.output.messages` | any (JSON) | No | Model response messages |
| `gen_ai.system_instructions` | any (JSON) | No | System prompt/instructions |

**Message Format:**
```json
{
  "gen_ai.input.messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "gen_ai.output.messages": [
    {"role": "assistant", "content": "Hi there! How can I help?"}
  ]
}
```

#### Tool/Function Calling Attributes

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.tool.name` | string | No | Tool/function name | `get_weather` |
| `gen_ai.tool.description` | string | No | Tool description | `Gets current weather` |
| `gen_ai.tool.call.id` | string | No | Tool call identifier | `call_abc123` |
| `gen_ai.tool.call.arguments` | any (JSON) | No | Arguments passed to tool | `{"city": "NYC"}` |
| `gen_ai.tool.call.result` | any (JSON) | No | Tool execution result | `{"temp": 72}` |
| `gen_ai.tool.definitions` | any (JSON) | No | Available tool definitions | Array of tool schemas |

#### Context Attributes

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.conversation.id` | string | No | Session/conversation ID | `conv_123` |
| `gen_ai.prompt.name` | string | No | Named prompt identifier | `customer_support_v2` |

#### Agent Attributes

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.agent.id` | string | No | Unique agent identifier | `agent_001` |
| `gen_ai.agent.name` | string | No | Human-readable agent name | `CustomerSupportAgent` |
| `gen_ai.agent.description` | string | No | Agent description | `Handles customer queries` |

#### Evaluation Attributes

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.evaluation.name` | string | No | Evaluation metric name | `relevance` |
| `gen_ai.evaluation.score.value` | double | No | Numeric score | `0.95` |
| `gen_ai.evaluation.score.label` | string | No | Label/category | `PASS` |
| `gen_ai.evaluation.explanation` | string | No | Score explanation | `Response was relevant` |

#### Embeddings Attributes

| Attribute | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `gen_ai.embeddings.dimension.count` | int | No | Embedding dimensions | `1536` |
| `gen_ai.request.encoding_formats` | string[] | No | Requested formats | `["float", "base64"]` |

---

## Operation Names

The `gen_ai.operation.name` attribute should be one of:

| Operation | Description | Example Providers |
|-----------|-------------|-------------------|
| `chat` | Chat completion | OpenAI, Anthropic, Google |
| `text_completion` | Text completion | OpenAI (legacy), Cohere |
| `embeddings` | Generate embeddings | OpenAI, Cohere, Voyage |
| `image_generation` | Generate images | OpenAI DALL-E, Stability |
| `speech_to_text` | Transcription | OpenAI Whisper |
| `text_to_speech` | Speech synthesis | OpenAI TTS, ElevenLabs |
| `invoke_agent` | Agent invocation | Custom agents |
| `execute_tool` | Tool execution | Function calling |

---

## Provider Names

The `gen_ai.provider.name` attribute should be one of:

| Provider | Value |
|----------|-------|
| OpenAI | `openai` |
| Anthropic | `anthropic` |
| Google (Gemini) | `google` |
| Google Vertex AI | `gcp.vertex_ai` |
| AWS Bedrock | `aws.bedrock` |
| Azure OpenAI | `azure.openai` |
| Cohere | `cohere` |
| Mistral AI | `mistralai` |
| Groq | `groq` |
| Together AI | `together` |
| Ollama | `ollama` |
| Hugging Face | `huggingface` |

---

## Migration from OpenInference (llm.*)

If migrating from OpenInference conventions, use this mapping:

| OpenInference (Old) | OTEL GenAI (New) |
|---------------------|------------------|
| `llm.model_name` | `gen_ai.request.model` |
| `llm.provider` | `gen_ai.provider.name` |
| `llm.system` | `gen_ai.provider.name` |
| `llm.input_messages` | `gen_ai.input.messages` |
| `llm.output_messages` | `gen_ai.output.messages` |
| `llm.token_count.prompt` | `gen_ai.usage.input_tokens` |
| `llm.token_count.completion` | `gen_ai.usage.output_tokens` |
| `llm.token_count.total` | *(computed: input + output)* |
| `llm.invocation_parameters` | Individual `gen_ai.request.*` attributes |
| `llm.tools` | `gen_ai.tool.definitions` |
| `llm.function_call` | `gen_ai.tool.call.*` |
| `session.id` | `gen_ai.conversation.id` |
| `fi.span.kind` | `gen_ai.operation.name` |
| `input.value` | `gen_ai.input.messages` (for chat) |
| `output.value` | `gen_ai.output.messages` (for chat) |

---

## Example Traces

### Chat Completion

```json
{
  "name": "chat gpt-4o-mini",
  "kind": "CLIENT",
  "attributes": {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "openai",
    "gen_ai.request.model": "gpt-4o-mini",
    "gen_ai.request.temperature": 0.7,
    "gen_ai.request.max_tokens": 1024,
    "gen_ai.response.model": "gpt-4o-mini-2024-07-18",
    "gen_ai.response.id": "chatcmpl-abc123",
    "gen_ai.response.finish_reasons": ["stop"],
    "gen_ai.usage.input_tokens": 25,
    "gen_ai.usage.output_tokens": 150,
    "gen_ai.input.messages": [
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "gen_ai.output.messages": [
      {"role": "assistant", "content": "The capital of France is Paris."}
    ]
  }
}
```

### Embeddings

```json
{
  "name": "embeddings text-embedding-3-small",
  "kind": "CLIENT",
  "attributes": {
    "gen_ai.operation.name": "embeddings",
    "gen_ai.provider.name": "openai",
    "gen_ai.request.model": "text-embedding-3-small",
    "gen_ai.response.model": "text-embedding-3-small",
    "gen_ai.usage.input_tokens": 10,
    "gen_ai.embeddings.dimension.count": 1536
  }
}
```

### Tool/Function Calling

```json
{
  "name": "chat gpt-4o",
  "kind": "CLIENT",
  "attributes": {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "openai",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.tool.definitions": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get current weather for a location",
          "parameters": {
            "type": "object",
            "properties": {
              "location": {"type": "string"}
            }
          }
        }
      }
    ],
    "gen_ai.output.messages": [
      {
        "role": "assistant",
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Paris\"}"
            }
          }
        ]
      }
    ]
  }
}
```

---

## FutureAGI Extensions

While following OTEL standards, FutureAGI adds some additional attributes for enhanced functionality:

| Attribute | Type | Description |
|-----------|------|-------------|
| `fi.span.kind` | string | FutureAGI span classification (LLM, TOOL, CHAIN, AGENT, etc.) |
| `fi.evaluation.*` | various | FutureAGI evaluation attributes |
| `fi.cost.total` | double | Computed cost in USD |
| `fi.cost.input` | double | Input token cost in USD |
| `fi.cost.output` | double | Output token cost in USD |

These extensions are additive and don't conflict with OTEL standards.

---

## References

- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [GenAI Attributes Registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- [GenAI Span Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/)
- [OpenLLMetry GitHub](https://github.com/traceloop/openllmetry)
- [OTEL Semantic Conventions GitHub](https://github.com/open-telemetry/semantic-conventions)

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2025-02-04 | Initial OTEL GenAI convention adoption |
