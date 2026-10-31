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

## BytePlus ModelArk

Use the same `OpenAIInstrumentor` with BytePlus's OpenAI-compatible Chat API;
no separate BytePlus instrumentation package is needed. Set `ARK_API_KEY` and
`ARK_MODEL` to your ModelArk platform key and model or endpoint ID, alongside
`FI_API_KEY` and `FI_SECRET_KEY` for Future AGI.

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="byteplus-chat",
)
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

try:
    with OpenAI(
        api_key=os.environ["ARK_API_KEY"],
        base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
    ) as client:
        response = client.chat.completions.create(
            model=os.environ["ARK_MODEL"],
            messages=[{"role": "user", "content": "Suggest a name for a travel app."}],
        )
        print(response.choices[0].message.content)
finally:
    trace_provider.force_flush()
    trace_provider.shutdown()
```

The [documented regional hosts](https://docs.byteplus.com/docs/ModelArk/2191806)
`ark.ap-southeast.bytepluses.com` (Johor) and `ark.eu-west.bytepluses.com`
(Dublin) are attributed to `gen_ai.provider.name=byteplus`. Use the host for
your key and endpoint's region. The `/api/v3` endpoint is ModelArk's platform
API, not the Coding Plan endpoint; the wrong endpoint can incur separate charges.

This recipe covers Python Chat Completions: synchronous and asynchronous calls,
streaming text, non-streaming tool-call outputs, and token usage when returned.
For streaming usage, pass `stream_options={"include_usage": True}` and consume
the stream. Streaming tool-call spans include tool definitions and usage, but
the shared instrumentor does not currently record their generated tool arguments.
Tool execution needs its own instrumentation. This is not instrumentation for
`arkruntime.Ark`, reasoning content, or other proprietary ModelArk APIs.

Prompts, completions, tool definitions and request parameters are captured by
default. Review [traceAI privacy settings](https://docs.futureagi.com/docs/tracing)
before sending sensitive content.

