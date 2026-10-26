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
| `hide_error_details` | bool | derived | Hide error messages and stacktraces (Python). Unset: on when any of `hide_inputs`, `hide_outputs`, `hide_input_messages`, `hide_output_messages`, `hide_input_text`, `hide_output_text` is on |

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
export FI_HIDE_ERROR_DETAILS=true
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
    hide_error_details=True,
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

## Error Details

Error text is content too: provider error bodies can quote the prompt back and carry
request or session ids, and Python tracebacks carry local file paths.

With `hide_error_details` on (Python), a failed span keeps:
- status `ERROR`, with no description
- `error.type`: the provider error code (for example `model_not_found`) or the fully
  qualified exception class
- exception events with `exception.type` (and `exception.escaped`) only

and drops `exception.message`, `exception.stacktrace`, `error.message` and
instrumentor keys ending in `.error.message` / `.error_message`. A failed LangChain run
also records no outputs: LangChain fills them with the provider's error response
(error body, response headers, request ids).

If you don't set `hide_error_details` (or `FI_HIDE_ERROR_DETAILS`), it is on whenever
any of `hide_inputs`, `hide_outputs`, `hide_input_messages`, `hide_output_messages`,
`hide_input_text` or `hide_output_text` is on. Set it to `false` explicitly to keep
error messages while hiding inputs and outputs:

```python
config = TraceConfig(
    hide_inputs=True,
    hide_outputs=True,
    hide_error_details=False,  # keep error messages for debugging
)
```

With it off, the status description is the exception message only, never a traceback;
the stacktrace is recorded on the exception event.

`error.type` is set only on spans that end with status `ERROR` (an exception recorded on
a span that still succeeds, such as a retried timeout, does not set it). It is the
provider error code for exceptions from openai, anthropic, groq, litellm,
botocore/boto3, google-genai, google-api-core and mistralai (for example
`model_not_found`, `not_found_error`, `ValidationException`, `NOT_FOUND`), the HTTP
status for httpx `HTTPStatusError` (`"404"`), and the fully qualified exception class
for everything else (for example `sqlalchemy.exc.OperationalError`).

## What's Always Captured

Even with maximum redaction, these are always captured:
- Span names and types
- Timestamps and duration
- Model names
- Token counts
- Error status and `error.type` (error messages are hidden with `hide_error_details`)

## Best Practices

1. **Start permissive in development** - Capture everything for debugging
2. **Restrict in production** - Hide PII and sensitive content
3. **Keep model parameters** - Useful for performance analysis
4. **Always capture token counts** - Essential for cost tracking
5. **Review compliance requirements** - GDPR, HIPAA, etc. may have specific rules

## Related

- [Environment Variables](environment-variables.md) - All configuration options
- [Context Managers](../python/context-managers.md) - Adding metadata
