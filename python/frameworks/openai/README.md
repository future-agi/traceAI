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
Instrument the OpenAI client before making requests. Use the same entry point for synchronous and asynchronous clients.

```python
from traceai_openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create OpenAI Components
Set up your OpenAI client with built-in observability.  

```python
from openai import OpenAI

client = OpenAI()  # Reads OPENAI_API_KEY from the environment.

response = client.chat.completions.create(
    model=os.environ["OPENAI_MODEL"],
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Can you tell me a joke?"}
    ]
)

print(response.choices[0].message.content)
client.close()
trace_provider.force_flush()
trace_provider.shutdown()
```

Set `OPENAI_MODEL` to a chat model available to your account. In a long-running
application, shut down the provider only when the application stops.

## Transcribe audio

After registering the provider and instrumenting the client, use a parsed
transcription response. The instrumentor records `gen_ai.operation.name=transcription`
and the input, output and total token counts reported by the API. Missing usage
and duration-based billing do not produce token counts; no tokens are estimated.
Uploaded audio bytes are not added to spans.

```python
from openai import OpenAI

try:
    with OpenAI() as client, open("meeting.wav", "rb") as audio:
        transcription = client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=audio,
            response_format="json",
        )
        print(transcription.text)
finally:
    trace_provider.force_flush()
    trace_provider.shutdown()
```

For `stream=True`, consume the returned stream in `with stream:` (or
`async with stream:` for `AsyncOpenAI`). The span finishes on exhaustion, explicit
`close()`, or context exit, including early exits and errors. The terminal
`transcript.text.done` event supplies the transcript and usage; stopping before
that event does not produce a completed transcript or estimated usage.

Request prompts and transcripts are content. Configure `TraceConfig(hide_inputs=True,
hide_outputs=True)` when calling `instrument()` to mask them while keeping token
counts. Error-detail masking additionally requires a fi-instrumentation-otel version
that supports `hide_error_details`.

## Python transport coverage

- Chat Completions and Responses HTTP calls, including their parsed SSE streams,
  use `OpenAIInstrumentor`.
- Transcription extraction supports parsed `json`, `verbose_json`, and, when
  available in the installed SDK, `diarized_json` responses, plus transcription
  SSE events. Token attributes are emitted only when the API reports token usage.
- `client.responses.connect()` uses WebSockets and is **not instrumented** by
  this package. Use `client.responses.create(..., stream=True)` for a traced
  streaming Responses request when an HTTP transport fits your application.
- The separate `traceAI-openai-realtime` integration targets `client.realtime.connect()`;
  it does not add coverage for Responses WebSockets.
- Raw `.with_streaming_response` wrappers, binary speech-download lifetimes,
  and text/SRT/VTT transcription formats are outside this audio extraction contract.
  Use the parsed JSON or SSE transcription route above for audio tracing.

