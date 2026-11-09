# OpenRouter Python SDK tracing

Trace calls made through the dedicated [`openrouter` Python SDK](https://pypi.org/project/openrouter/) with the maintained `traceAI-openrouter` package. For the OpenAI-compatible client, use `traceAI-openai` instead; do not instrument the same request with both packages or HTTPX tracing.

```bash
pip install traceAI-openrouter
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `OPENROUTER_API_KEY` in your environment. Then:

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openrouter import OpenRouter
from traceai_openrouter import OpenRouterInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="openrouter-demo")
OpenRouterInstrumentor().instrument(tracer_provider=provider)
client = OpenRouter(api_key=os.environ["OPENROUTER_API_KEY"])
answer = client.chat.send(
    model="openai/gpt-4o-mini",
    messages=[{"role": "user", "content": "What is the capital of Japan?"}],
)
print(answer.choices[0].message.content)
provider.force_flush()
```

Find the `openrouter.chat` span in your Future AGI project. The adapter covers synchronous and asynchronous Chat, Embeddings, Responses and deprecated `beta.responses` calls. Streams finish when fully consumed or closed; final usage is available only when the server sends a usage chunk. For early exits, close the stream, or use a context manager.

The default TraceConfig captures plain-text chat messages, plain-text Responses inputs and textual outputs, capped at 32 KiB. Structured or multimodal input, embedding vectors and content, image payloads, exception messages and stacks, and authorization headers are never included. Pass `TraceConfig(hide_inputs=True, hide_outputs=True)` to `instrument(config=...)` to remove prompt/response content. Directional message, text and image privacy flags also suppress the corresponding generic input/output attributes; hiding input images suppresses the entire input value because a text string can contain an image reference. The package does not send requests to OpenRouter on its own.
