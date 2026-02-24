# TraceConfig - Privacy & Data Redaction

TraceConfig controls what data is captured in traces. Use it to protect sensitive information and comply with privacy requirements.

## Overview

By default, traceAI captures all data including prompts, completions, and model parameters. TraceConfig lets you selectively hide this data.

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `hide_inputs` | bool | `false` | Hide all input values and messages |
| `hide_outputs` | bool | `false` | Hide all output values and messages |
| `hide_input_messages` | bool | `false` | Hide input message content only |
| `hide_output_messages` | bool | `false` | Hide output message content only |
| `hide_input_images` | bool | `false` | Hide base64 images in inputs |
| `hide_input_text` | bool | `false` | Hide text content in inputs |
| `hide_output_text` | bool | `false` | Hide text content in outputs |
| `hide_embedding_vectors` | bool | `false` | Hide embedding vector values |
| `hide_llm_invocation_parameters` | bool | `false` | Hide model parameters (temperature, etc.) |
| `base64_image_max_length` | int | `32000` | Truncate large base64 images |

## Python Usage

### Via Environment Variables

```bash
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_INPUT_MESSAGES=true
export FI_HIDE_OUTPUT_MESSAGES=true
export FI_HIDE_INPUT_IMAGES=true
export FI_HIDE_INPUT_TEXT=true
export FI_HIDE_OUTPUT_TEXT=true
export FI_HIDE_EMBEDDING_VECTORS=true
export FI_HIDE_LLM_INVOCATION_PARAMETERS=true
export FI_BASE64_IMAGE_MAX_LENGTH=16000
```

### Via Code

```python
from fi_instrumentation.instrumentation.config import TraceConfig

config = TraceConfig(
    hide_inputs=True,
    hide_outputs=True,
    hide_input_messages=True,
    hide_output_messages=True,
    hide_input_images=True,
    hide_input_text=True,
    hide_output_text=True,
    hide_embedding_vectors=True,
    hide_llm_invocation_parameters=True,
    base64_image_max_length=16000,
)
```

## TypeScript Usage

```typescript
import { OpenAIInstrumentation } from "@traceai/openai";

const instrumentation = new OpenAIInstrumentation({
    traceConfig: {
        hideInputs: true,
        hideOutputs: true,
        hideInputMessages: true,
        hideOutputMessages: true,
        hideInputImages: true,
        hideInputText: true,
        hideOutputText: true,
        hideEmbeddingVectors: true,
        base64ImageMaxLength: 16000,
    }
});
```

## Configuration Precedence

Configuration is applied in this order (later overrides earlier):

1. Default values
2. Environment variables
3. Programmatic configuration

## Common Use Cases

### Production Privacy

Hide all user content but keep model parameters:

```python
config = TraceConfig(
    hide_inputs=True,
    hide_outputs=True,
    hide_llm_invocation_parameters=False,  # Keep for debugging
)
```

### PII Protection

Hide text content but keep message structure:

```python
config = TraceConfig(
    hide_input_text=True,
    hide_output_text=True,
    # Messages will show roles but "__REDACTED__" for content
)
```

### Large Image Handling

Truncate large images to reduce trace size:

```python
config = TraceConfig(
    base64_image_max_length=10000,  # Truncate images > 10KB
    hide_input_images=False,        # Keep small images
)
```

### Embedding Privacy

Hide vector values for embedding operations:

```python
config = TraceConfig(
    hide_embedding_vectors=True,  # Vectors shown as "__REDACTED__"
)
```

### Complete Redaction

Hide everything for maximum privacy:

```python
config = TraceConfig(
    hide_inputs=True,
    hide_outputs=True,
    hide_llm_invocation_parameters=True,
    hide_embedding_vectors=True,
)
```

## Redacted Values

When data is hidden, it's replaced with `"__REDACTED__"`:

```json
{
    "llm.input_messages": [
        {
            "role": "user",
            "content": "__REDACTED__"
        }
    ],
    "llm.output_messages": [
        {
            "role": "assistant",
            "content": "__REDACTED__"
        }
    ]
}
```

## What's Always Captured

Even with maximum redaction, these are always captured:
- Span names and types
- Timestamps and duration
- Model names
- Token counts
- Error messages (consider additional error handling)

## Best Practices

1. **Start permissive in development** - Capture everything for debugging
2. **Restrict in production** - Hide PII and sensitive content
3. **Keep model parameters** - Useful for performance analysis
4. **Always capture token counts** - Essential for cost tracking
5. **Review compliance requirements** - GDPR, HIPAA, etc. may have specific rules

## Related

- [Environment Variables](environment-variables.md) - All configuration options
- [Context Managers](../python/context-managers.md) - Adding metadata
