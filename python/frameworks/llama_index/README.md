# LlamaIndex OpenTelemetry integration

Trace LlamaIndex model calls in Future AGI with the maintained `LlamaIndexInstrumentor`.

## Install

Use Python 3.10–3.13. Install the instrumentor and the LlamaIndex components used by this example:

```bash
pip install "traceAI-llamaindex>=0.1.10" llama-index-core llama-index-llms-openai
```

## Configure credentials

Set your Future AGI credentials and your own OpenAI API key in the environment before running Python:

```bash
export FI_API_KEY="<your-future-agi-api-key>"
export FI_SECRET_KEY="<your-future-agi-secret-key>"
export OPENAI_API_KEY="<your-openai-api-key>"
```

## Run a chat request

Register the trace provider and instrument LlamaIndex before creating the model. This example hides prompt and response content while retaining model and token-usage metadata.

```python
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from llama_index.core.llms import ChatMessage
from llama_index.llms.openai import OpenAI
from traceai_llamaindex import LlamaIndexInstrumentor

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="llama_index_app",
)
LlamaIndexInstrumentor().instrument(
    tracer_provider=trace_provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)

try:
    llm = OpenAI(model="gpt-4o-mini")
    response = llm.chat([
        ChatMessage(role="user", content="Suggest a name for a community garden."),
    ])
    print(response.message.content)
finally:
    trace_provider.shutdown()
```

Open the `llama_index_app` project in Future AGI to inspect the `OpenAI.chat` LLM span, its duration and token usage. Prompt and response content are hidden by the configuration above.

Use one tracing layer for these calls: do not also enable a separate OpenAI instrumentor for the same LlamaIndex model calls if you want to avoid duplicate model spans.

For standalone workflow run and step tracing, use the same instrumentor with the [LlamaIndex Workflows guide](https://docs.futureagi.com/docs/integrations/traceai/llamaindex-workflows).
