# traceAI-google-generativeai

OpenTelemetry instrumentation for the legacy Google Generative AI Python SDK (`google-generativeai`, imported as `google.generativeai`). Applications on the newer `google-genai` SDK use `traceAI-google-genai` instead.

## Installation

```bash
pip install traceAI-google-generativeai
```

## Usage

```python
import google.generativeai as genai
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_google_generativeai import GoogleGenerativeAIInstrumentor

tracer_provider = register(project_name="my-project", project_type=ProjectType.OBSERVE)
GoogleGenerativeAIInstrumentor().instrument(tracer_provider=tracer_provider)

genai.configure(api_key="...")
model = genai.GenerativeModel("gemini-1.5-flash")
print(model.generate_content("Say hello").text)
```

With `FI_INSTRUMENTATIONS` unset, the zero-code agent enables it automatically when `google.generativeai` is importable.

## What is traced

| Call | Span | Kind |
| --- | --- | --- |
| `GenerativeModel.generate_content`, with or without `stream=True` | `GenerateContent` | LLM |
| `GenerativeModel.generate_content_async`, with or without `stream=True` | `AsyncGenerateContent` | LLM |
| `ChatSession.send_message` / `send_message_async` | one `GenerateContent` span per turn, chat history as input | LLM |
| `embed_content` / `embed_content_async` | `EmbedContent` / `AsyncEmbedContent` | EMBEDDING |

`count_tokens` is not traced.

LLM spans carry the model, generation parameters (`gen_ai.request.parameters`, temperature, top-p, top-k, max tokens, stop sequences), the system instruction, input and output messages, tool definitions, tool calls with their JSON arguments, token usage (input, output, total, cached), finish reasons and the aggregated response. Streaming spans end when the stream is consumed, record `gen_ai.server.time_to_first_token` (milliseconds) and the aggregated output; a stream that is dropped part-way ends with `gen_ai.response.incomplete`. Failed calls re-raise unchanged and mark the span as an error. Embedding spans record the input texts and the vector size, never the vectors. Inline binary parts are noted by MIME type and size, never copied.

## Privacy

Content goes through the `TraceConfig` masker. `hide_inputs` / `FI_HIDE_INPUTS` removes prompts, system instructions, tool definitions, function results and embedding texts; `hide_outputs` / `FI_HIDE_OUTPUTS` removes responses and tool-call arguments.

## Supported versions

`google-generativeai` 0.8.0 and later (tested on 0.8.x), Python 3.10 to 3.13. `embed_content` imported with `from google.generativeai import embed_content` before instrumenting keeps the original function; import it after instrumenting or call it through `genai`.
