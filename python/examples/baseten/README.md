# Baseten (OpenAI-compatible) with traceAI

Trace Chat Completions made through the official `openai` Python SDK at
`https://inference.baseten.co/v1`. This recipe uses `traceai-openai`; it does
not need a Baseten tracing package.

## Install

From this directory, install the tested pins:

```bash
pip install -r requirements.txt
```

The plain install form is:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

## Configure

| Environment variable | Purpose | Destination |
| --- | --- | --- |
| `FI_API_KEY` | Future AGI API key | Tracer only |
| `FI_SECRET_KEY` | Future AGI secret key | Tracer only |
| `BASETEN_API_KEY` | Baseten API key | OpenAI client only |
| `BASETEN_MODEL` | Required Model APIs slug; `--model` overrides it | Baseten request |
| `BASETEN_BASE_URL` | Optional override; defaults to `https://inference.baseten.co/v1` | OpenAI client |
| `FI_BASE_URL` | Optional Future AGI collector origin | Tracer only |

Choose a slug from Baseten's current [Model APIs table](https://docs.baseten.co/inference/model-apis/overview).
`zai-org/GLM-5.2` is a documentation example, not a model availability guarantee.

## Run

Run from this directory. Replace the placeholders with your own keys:

```bash
export FI_API_KEY="placeholder-futureagi-api-key"
export FI_SECRET_KEY="placeholder-futureagi-secret-key"
export BASETEN_API_KEY="placeholder-baseten-key"
export BASETEN_MODEL="zai-org/GLM-5.2"
python src/app.py
python src/app.py --stream
```

You can also pass `--model <slug>` and `--prompt "What is the capital of France?"`.
The app flushes traces before returning. The SDK appends a trailing slash to
`client.base_url`: `https://inference.baseten.co/v1/`. Its request goes to
`https://inference.baseten.co/v1/chat/completions`.

## Code

Instrument before creating the client. Future AGI keys are read by the tracer;
only the Baseten key is passed to `OpenAI`:

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor
provider = register(project_name="baseten-openai", project_type=ProjectType.OBSERVE,
                    set_global_tracer_provider=False, verbose=False)
OpenAIInstrumentor().instrument(tracer_provider=provider)
with OpenAI(base_url="https://inference.baseten.co/v1",
            api_key=os.environ["BASETEN_API_KEY"]) as client:
    response = client.chat.completions.create(model=os.environ["BASETEN_MODEL"],
        messages=[{"role": "user", "content": "What is the capital of France?"}])
    print(response.choices[0].message.content)
provider.force_flush()
```

See [src/app.py](src/app.py) for the runnable version and URL checks.

## What you see in Future AGI

For this Chat Completions flow, expect one `ChatCompletion` LLM span per call.
On normal calls, `gen_ai.request.model` is the model id the provider returns
in its response. Token usage appears only when the response has `usage`;
missing usage is omitted, never reported as zero.

Streamed calls record accumulated output text but have no model attribute.
The default stream tested here returns no usage, so it exports
no `gen_ai.usage.*` attributes. Failed calls produce an error span with an
exception event and no model attribute. These are current `traceai-openai` behaviour.

The provider field says `openai`: `gen_ai.provider.name` is `openai`. This is
the shared instrumentor's label, including when the client calls Baseten.

## Provider specifics

This recipe covers Baseten Model APIs only. Dedicated deployments use their
own per-deployment URL and are not covered. The app rejects hosts matching
`model-*.api.baseten.co`, including
`https://model-abc123.api.baseten.co/environments/production/sync/v1`.

The Anthropic Messages beta at `https://inference.baseten.co` uses a different
client and is not covered. The app rejects that root and tells you to use
`/v1` for the OpenAI SDK. It returns allowed URLs unchanged, including custom
proxy URLs; it does not rewrite rejected URLs. Baseten CLI is out of scope.

`x-session-affinity` is a Baseten routing header. It is NOT a Future AGI
session id; there is no bridge between them. For a Future AGI session, use
the context helper exported by `fi_instrumentation`:

```python
from fi_instrumentation import using_session

with using_session("s-1"):
    response = client.chat.completions.create(
        model=os.environ["BASETEN_MODEL"],
        messages=[{"role": "user", "content": "Hello"}],
    )
```

After tracing setup, this context adds `session.id="s-1"` to the span.

## Privacy

Set `FI_HIDE_INPUTS=true` to keep prompt text out of exported spans. Set
`FI_HIDE_OUTPUTS=true` to keep response text out. Set these before tracing
setup. The provider still receives the prompt. Future AGI masking does not
change Baseten's own logging.

## Limits / not covered

- Anthropic SDK calls to Baseten and its Anthropic Messages beta.
- Dedicated deployments and the Baseten CLI.
- Historical import of past requests.
- Not tested against the live provider (no paid call); the tests use a local fake of the OpenAI API.

## Tests

The tests pin the current absence of model attributes on streaming and error
spans. They also pin the absence of usage attributes on the default stream.

From the repository root:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/baseten/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/baseten/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

The tests use MockTransport at the documented host and local HTTP servers.
Subprocesses install a socket guard that blocks external hosts before DNS.

| Python | `openai` | traceAI packages | Result |
| --- | --- | --- | --- |
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |

To run another row, change the `openai` pin in the command above, or replace the
repository paths on `PYTHONPATH` with `--with 'traceAI-openai==0.1.10' --with
'fi-instrumentation-otel==1.1.0'`.
