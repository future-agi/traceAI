# OpenAI OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the OpenAI framework. It enables tracing and monitoring of applications built with OpenAI.

## Installation

1. **Install traceAI OpenAI**

```bash
pip install traceAI-openai
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="openai_app"
)
```

### Configure OpenAI Instrumentation
Set up your OpenAI client with built-in observability. This includes support for text, image, and audio models.

```python
from traceai_openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create OpenAI Components
Set up your OpenAI client with built-in observability.  

```python
import openai

openai.api_key = os.environ["OPENAI_API_KEY"]

response = openai.ChatCompletion.create(
    model="gpt-4-0",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Can you tell me a joke?"}
    ]
)

print(response.choices[0].message['content'].strip())
```

## MiniMax Chat Completions

MiniMax's [OpenAI-compatible API](https://platform.minimax.io/docs/api-reference/text-openai-api)
uses the same `OpenAIInstrumentor`; no second instrumentor is needed. With the
tracer provider configured above, make a text request as follows:

```python
import os

from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

with OpenAI(
    api_key=os.environ["MINIMAX_API_KEY"],
    base_url="https://api.minimax.io/v1",
) as client:
    response = client.chat.completions.create(
        model=os.environ["MINIMAX_MODEL"],
        messages=[{"role": "user", "content": "Give me three names for a weather app."}],
    )
    print(response.choices[0].message.content)
```

Set `MINIMAX_API_KEY` to your MiniMax key and `MINIMAX_MODEL` to a model available
to your account. The international endpoint is `https://api.minimax.io/v1`;
the [mainland China endpoint](https://platform.minimax.cn/docs/api-reference/text-openai-api)
is `https://api.minimax.cn/v1`. These exact hosts, plus the legacy
`api.minimaxi.com` and `api.minimax.chat` hosts, produce
`gen_ai.provider.name=minimax`. Custom proxies retain the `openai` attribution;
a MiniMax model name alone does not identify the serving provider.

Enable only one instrumentor for a request. Combining `OpenAIInstrumentor` and
`MiniMaxInstrumentor` produces duplicate model spans. The dedicated
`traceAI-minimax` package is **not deprecated** by this setup: the shared route
requires OpenAI SDK 1.69 or later and is not yet a field-for-field replacement.
If you depend on the dedicated package's response IDs or finish reasons, keep
that integration until those fields have a migration path. The shared route
also does not yet export streamed tool arguments and can omit streamed text
when the final chunk has null usage; use non-streaming calls for these cases.

