# IBM watsonx.ai Python SDK tracing

Trace calls made through the [`ibm-watsonx-ai` Python SDK](https://pypi.org/project/ibm-watsonx-ai/) with the maintained `traceAI-watsonx` package. It supports `ibm-watsonx-ai` 1.3.0 up to, but not including, 2.0.

```bash
pip install traceAI-watsonx
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and your watsonx.ai credentials (`WATSONX_APIKEY`, `WATSONX_URL`, `WATSONX_PROJECT_ID`) in your environment. Then:

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from ibm_watsonx_ai import Credentials
from ibm_watsonx_ai.foundation_models import ModelInference
from traceai_watsonx import WatsonxInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="watsonx-demo")
WatsonxInstrumentor().instrument(tracer_provider=provider)
model = ModelInference(
    model_id="ibm/granite-3-8b-instruct",
    credentials=Credentials(api_key=os.environ["WATSONX_APIKEY"], url=os.environ["WATSONX_URL"]),
    project_id=os.environ["WATSONX_PROJECT_ID"],
)
answer = model.chat(messages=[{"role": "user", "content": "What is the capital of Japan?"}])
print(answer["choices"][0]["message"]["content"])
provider.force_flush()
```

With the `fi-instrument` launcher or `FI_INSTRUMENTATIONS`, no code is needed: importing `ibm_watsonx_ai` turns this integration on.

Find the `watsonx.chat` span in your Future AGI project. Covered calls:

| Span | SDK methods |
| --- | --- |
| `watsonx.text_completion` | `ModelInference.generate`, `generate_text`, `generate_text_stream`, `agenerate`, `agenerate_stream` |
| `watsonx.chat` | `ModelInference.chat`, `chat_stream`, `achat`, `achat_stream` |
| `watsonx.embeddings` | `Embeddings.generate`, `embed_documents`, `embed_query`, `agenerate`, `aembed_documents`, `aembed_query` |

Each span records `gen_ai.provider.name` (`ibm.watsonx.ai`), the request model or deployment id, request parameters, input and output messages, token usage (from `input_token_count` / `generated_token_count` or the chat `usage`), and finish reasons. `generate_text` returns only the text, so its span has no token counts; use `generate` or `generate_text(raw_response=True)` to get them. One SDK call is one span: methods that call each other inside the SDK (`generate_text` calls `generate`, `embed_query` calls `generate`) are not traced twice.

Streams finish when fully consumed or closed; a stream abandoned without closing it is marked `watsonx.stream.incomplete` and carries no partial output. Errors set the span status to ERROR and `error.type`, and the exception is re-raised unchanged. Exception messages, stacks, credentials and embedding vectors are never exported. Non-text parts of chat messages (images) are not captured.

Pass `TraceConfig(hide_inputs=True, hide_outputs=True)` to `instrument(config=...)` to remove prompt and response content. Do not instrument HTTPX for the same requests, or you will get duplicate spans. The package does not send requests to IBM on its own.
