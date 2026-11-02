# PrismML (OpenAI-compatible) with traceAI

Trace calls to your local [PrismML Bonsai server](https://docs.prismml.com/run/server) in Future AGI using the maintained `traceai-openai` package. This recipe uses the OpenAI Python SDK's Chat Completions API; it is not an integration for other products named Prism.

## Install

Use Python 3.10–3.13. From this recipe directory:

```bash
python -m pip install -r requirements.txt
```

For an existing application, install the same packages directly:

```bash
python -m pip install 'openai==3.42.1' 'traceAI-openai==0.1.12' 'fi-instrumentation-otel==1.2.2'
```

## Start the local server

Follow the [PrismML server setup](https://docs.prismml.com/run/server) to download a model and start the llama.cpp server. In the Bonsai demo directory, give the loaded model a stable name:

```bash
./scripts/start_llama_server.sh --alias bonsai
```

The API base URL is `http://localhost:8080/v1`. Keep the server on loopback: it does not authenticate requests. Binding to a non-loopback address exposes it to your network; use a trusted network or firewall the port.

On Apple Silicon, PrismML also provides `./scripts/start_mlx_server.sh` at `http://localhost:8081/v1`. Follow its [sampling and thinking configuration](https://docs.prismml.com/run/server); unlike the llama.cpp launcher, the MLX server expects those settings in each request.

## Configure

Set your Future AGI credentials in the shell that runs the Python client:

```bash
export FI_API_KEY="YOUR_FUTURE_AGI_API_KEY"
export FI_SECRET_KEY="YOUR_FUTURE_AGI_SECRET_KEY"
export FI_PROJECT_NAME="prismml-bonsai"
export PRISMML_BASE_URL="http://localhost:8080/v1"
export PRISMML_MODEL="bonsai"
```

The tracer sends OTLP to `https://api.futureagi.com/tracer/v1/traces` by default. The OpenAI client sends model requests only to `PRISMML_BASE_URL`. Do not use your Future AGI credentials as the local server key.

| Variable | Value or default | Purpose |
|---|---|---|
| `FI_API_KEY` | Your Future AGI API key | Authenticate trace export |
| `FI_SECRET_KEY` | Your Future AGI secret key | Authenticate trace export |
| `FI_PROJECT_NAME` | Optional; `prismml-bonsai` | Observe project name |
| `FI_BASE_URL` | Optional; `https://api.futureagi.com` | Trace export origin; omit `/tracer/v1/traces` |
| `PRISMML_BASE_URL` | Optional; `http://localhost:8080/v1` | Local server API base URL |
| `PRISMML_MODEL` | Optional; `bonsai` | Requested model name |
| `PRISMML_API_KEY` | Optional; `not-needed` | Non-empty placeholder required by the OpenAI SDK |

## Run

Return to this recipe directory and run a normal or streamed completion:

```bash
python src/app.py --prompt "What is the capital of France?"
python src/app.py --stream --prompt "What is the capital of France?"
```

The application prints the answer and flushes traces before exiting. If a stream ends without a finish reason, it exits with status 1; treat any printed text as a partial answer and retry. `--model` overrides `PRISMML_MODEL`. An unset or empty project name uses `prismml-bonsai`.

## Code

For your own application, register the tracer and instrument the SDK once, before creating the client. Do not add a second OpenAI instrumentor around the same requests.

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

provider = register(
    project_name=os.environ.get("FI_PROJECT_NAME") or "prismml-bonsai",
    project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=False,
    verbose=False,
)
OpenAIInstrumentor().instrument(tracer_provider=provider)
try:
    with OpenAI(
        base_url=os.environ.get("PRISMML_BASE_URL") or "http://localhost:8080/v1",
        api_key=os.environ.get("PRISMML_API_KEY") or "not-needed",
    ) as client:
        response = client.chat.completions.create(
            model=os.environ.get("PRISMML_MODEL") or "bonsai",
            messages=[{"role": "user", "content": "What is the capital of France?"}],
        )
        print(response.choices[0].message.content or "")
finally:
    provider.force_flush()
```

## View traces

Open **Observe** in Future AGI and select the project named by `FI_PROJECT_NAME`. Each completion produces one `ChatCompletion` LLM span with the prompt, answer, latency and any token usage supplied by the server. The provider field says `openai` because this local endpoint uses the shared OpenAI instrumentor.

- Non-streamed calls record the server's returned model in `gen_ai.request.model`. Streamed and failed calls record the requested model. The original requested name also appears in `gen_ai.request.parameters` unless invocation parameters are hidden.
- Streamed answer text is accumulated in `output.value`. To request streaming token counts, pass `stream_options={"include_usage": True}`; counts appear only if the server returns a final usage chunk. Missing usage is omitted, not replaced with zero.
- SDK errors mark the span as ERROR. Returned `tool_calls` are recorded, but the instrumentor does not execute tools.
- Local models may have no matching price entry, so cost can be unavailable even when token counts are present.

For llama.cpp thinking control, pass `extra_body={"thinking_budget_tokens": 0}` to `client.chat.completions.create()` to disable thinking for that request. This server-specific option reaches the model server but is not included in the pinned instrumentor's invocation parameters. See the [PrismML server reference](https://docs.prismml.com/run/server) for model-specific settings.

## Privacy

Model execution is local, but prompt and answer content are exported to Future AGI by default. To hide content and invocation parameters, set these variables before running the application:

```bash
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_LLM_INVOCATION_PARAMETERS=true
```

Hide both input and output if an answer might echo a sensitive prompt. These settings do not change what the local server receives or logs.

The returned model name is metadata and is not masked by these flags. Without an alias, llama-server can return the loaded model file's **absolute path**, including your user name or directory layout. Start it with `--alias bonsai` to keep local paths out of traces.

## Troubleshooting

- **Connection refused:** start the local server first and check the port. Use port 8080 for llama.cpp or 8081 for MLX.
- **URL validation error:** the recipe requires a base URL ending in `/v1` or `/v1/`, not the web chat root or `/v1/chat/completions`. Remove credentials, query strings and fragments. Compose host names such as `bonsai_server` are accepted; non-loopback HTTP prints a security warning.
- **No traces:** check `FI_API_KEY`, `FI_SECRET_KEY` and the project name. `FI_BASE_URL` is the trace export origin, not the model server URL.
- **Unexpected model path or missing cost:** use `--alias bonsai`; pricing still requires a matching model price entry.

This route traces calls made by your Python client. It does not automatically trace the server's built-in web chat or other applications connected to it.
