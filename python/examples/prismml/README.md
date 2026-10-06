# PrismML (OpenAI-compatible) with traceAI

PrismML here means the Bonsai local server from prismml.com; this recipe traces Chat Completions through the `openai` SDK at `http://localhost:8080/v1` ([server docs](https://docs.prismml.com/run/server)). It uses `traceai-openai`, with no PrismML instrumentation package.

Not these: other products named Prism are not covered here, for example the [ssimplifi Prism proxy guide](https://ssimplifi.com/guides/openai-compatible-api), [prism-proxy](https://git.sovereign-society.org/prism/prism-proxy) and [Prism PHP](https://mintlify.wiki/prism-php/prism/providers/overview).

## Install

From this recipe directory, install the pinned dependencies:

```bash
pip install -r requirements.txt
```

The plain installation form is:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

The test versions and verification scope are listed below.

## Configure

Start your server first, following the [PrismML server docs](https://docs.prismml.com/run/server): `./scripts/start_llama_server.sh` serves llama.cpp on port 8080; `./scripts/start_mlx_server.sh` serves MLX on Apple Silicon on port 8081. Both expose the same OpenAI-compatible API.

| Variable | Value or default | Destination |
|---|---|---|
| `FI_API_KEY` | Your Future AGI API key | Tracer only; never the local server |
| `FI_SECRET_KEY` | Your Future AGI secret key | Tracer only; never the local server |
| `PRISMML_API_KEY` | Optional; `not-needed` | OpenAI client only; never the Future AGI export |
| `PRISMML_MODEL` | Optional; `bonsai` | Model name sent by the OpenAI client |
| `PRISMML_BASE_URL` | Optional; `http://localhost:8080/v1` | OpenAI client; use `http://localhost:8081/v1` for MLX |
| `FI_BASE_URL` | Optional; the tracer's Future AGI collector default | Tracer export origin |
| `FI_HIDE_INPUTS` | Optional; `true` masks input attributes | Tracer only |
| `FI_HIDE_OUTPUTS` | Optional; `true` masks output attributes | Tracer only |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | Optional; `true` omits invocation parameters | Tracer only |

The local key is a placeholder, not a Future AGI secret. The OpenAI SDK requires a non-empty key, but the server does not check it ([server docs](https://docs.prismml.com/run/server)). An unset or empty `PRISMML_API_KEY` uses `not-needed`; an unset or empty `PRISMML_MODEL` uses `bonsai`.

The recipe validates `PRISMML_BASE_URL` before tracing. Invalid URLs exit 2 and name that variable. An explicitly empty `--model` exits 2 and names `PRISMML_MODEL`. The recipe does not validate the Future AGI keys.

## Run

From this recipe directory, with your server already running:

```bash
export FI_API_KEY="placeholder-fi-api-key"
export FI_SECRET_KEY="placeholder-fi-secret-key"
export PRISMML_API_KEY="not-needed"
export PRISMML_MODEL="bonsai"
export PRISMML_BASE_URL="http://localhost:8080/v1"
python src/app.py
python src/app.py --stream
```

Replace the Future AGI placeholders with your keys. `--prompt` changes the question and `--model` overrides `PRISMML_MODEL`. The app prints answer text and flushes the tracer.

## Code

Instrument before creating the client. Future AGI keys are read by the tracer from its environment.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor
provider = register(
    project_name="prismml-bonsai", project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=False, verbose=False,
)
OpenAIInstrumentor().instrument(tracer_provider=provider)
with OpenAI(base_url="http://localhost:8080/v1", api_key="not-needed") as client:
    response = client.chat.completions.create(
        model="bonsai", messages=[{"role": "user", "content": "Say hello."}], stream=False,
    )
    print(response.choices[0].message.content or "")
provider.force_flush()
```

## What you see in Future AGI

The fixtures produce one `ChatCompletion` span per call with `gen_ai.span.kind=LLM`. The provider field says `openai`: `gen_ai.provider.name` is the shared instrumentor's label.

Current `traceai-openai` behavior:

- For a successful non-streamed call, `gen_ai.request.model` is the model name the server returns. The fixture requests `bonsai` and returns the synthetic loaded-model name `Ternary-Bonsai-2-27B-Q4_K_M.gguf`; the latter is recorded. The requested `bonsai` remains in the `gen_ai.request.parameters` JSON.
- Streamed and failed calls omit `gen_ai.request.model`. The requested model remains in `gen_ai.request.parameters`, unless invocation parameters are hidden.
- Non-streamed token usage is recorded as `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and `gen_ai.usage.total_tokens` when the response supplies it. Missing usage is omitted, never replaced with zero.
- A stream accumulates answer text in `output.value`. Default fixture streams omit usage. With `stream_options={"include_usage": True}` and a final usage chunk, the same three token attributes are recorded. This verifies the instrumentor, not the real server's streaming usage support.
- Non-streamed `output.value` contains assistant content. Tool-call responses contain a readable function call instead. A stream with no text records `output.value=''`; Chat Completions have no raw-response fallback here.
- SDK errors set span status to ERROR and record an exception event. The 401 fixture is synthetic; it does not imply that this unauthenticated server checks a key.

A cost lookup keyed on the requested name may miss the server's returned name. Cost may be unknown when usage or a matching price entry is unavailable. This recipe adds no model pricing.

## Provider specifics

According to the [PrismML server docs](https://docs.prismml.com/run/server):

| Server | API base URL |
|---|---|
| llama.cpp | `http://localhost:8080/v1` |
| MLX on Apple Silicon | `http://localhost:8081/v1` |

The SDK appends a trailing slash to its stored base URL and calls `/v1/chat/completions`. The validator returns allowed URLs unchanged. It permits exactly `/v1` or `/v1/` on any host and refuses the root web chat, a full `/v1/chat/completions` URL, other paths, credentials, query or fragment delimiters, whitespace, control characters, non-ASCII hosts and malformed IDNA. Host names may use letters, digits, hyphens and underscores inside a label, so container or Compose service names such as `bonsai_server` work.

Loopback HTTP is allowed without a warning for `localhost` and loopback IP addresses (`127.0.0.0/8`, `::1`). HTTP outside loopback is allowed with one stderr warning about the unauthenticated server. Use it only on a trusted LAN. HTTPS URLs still need the same API path.

The [server docs](https://docs.prismml.com/run/server) warn:

> The scripts bind to `127.0.0.1`, so the server is reachable only from the same machine and there is no authentication. Setting `BONSAI_HOST` to any non-loopback address (for example `0.0.0.0`) exposes an unauthenticated server to your network.

For llama-server, any model string works because it serves the loaded model; `GET /v1/models` returns the real name. The documented Python example requests `bonsai` with streaming ([server docs](https://docs.prismml.com/run/server)). `traceai-openai` records the `model` field of the server's non-streamed response, pinned by the tests; the loaded-model name used in tests is synthetic, not a promised filename.

Thinking is controlled per request with `thinking_budget_tokens`; zero disables it ([server docs](https://docs.prismml.com/run/server)). Send it through the SDK's `extra_body`:

```python
response = client.chat.completions.create(
    model="bonsai", messages=[{"role": "user", "content": "Say hello."}],
    extra_body={"thinking_budget_tokens": 0},
)
```

The tests pin that this value reaches the HTTP request but is absent from `gen_ai.request.parameters`. The SDK merges `extra_body` after the instrumentor reads the request parameters.

The 27B model supports native OpenAI-style `tool_calls` ([server docs](https://docs.prismml.com/run/server)). The fixture uses a standard `tools` array. It records definitions in `gen_ai.tool.definitions` and `gen_ai.tool.definitions.0.tool.json_schema`, and records the returned tool ID, function name and arguments under `gen_ai.output.messages.0.message.tool_calls.0.tool_call.*`. It does not execute a tool.

## Privacy

Prompt and answer text normally leave the machine in the Future AGI trace export. Set `FI_HIDE_INPUTS=true` to mask input attributes and `FI_HIDE_OUTPUTS=true` to mask output attributes. The tests verify each flag against visible controls for both chat and streaming.

`FI_HIDE_INPUTS` masks only inputs. If the server echoes the prompt in its answer, that text remains in output attributes unless outputs are also hidden. Non-streamed output is assistant content, and streamed output is accumulated text, including an empty string when no text arrives. This recipe does not export a raw Chat Completions response in `output.value`.

The local server still receives the prompt. Future AGI masking does not change the server's memory or its own logging. Key separation tests verify that `not-needed` and the custom local placeholder never reach the Future AGI export, while Future AGI keys never reach the local request.

## Limits / not covered

- The built-in web chat, Open WebUI, MCP, Responses API, embeddings and other API surfaces.
- Starting or stopping the server, distributing weights, live GPU jobs, MLX server differences, benchmarks or model quality.
- Tool execution and streaming tool calls.
- Other products called Prism: the [ssimplifi Prism proxy guide](https://ssimplifi.com/guides/openai-compatible-api), [prism-proxy](https://git.sovereign-society.org/prism/prism-proxy), and [Prism PHP](https://mintlify.wiki/prism-php/prism/providers/overview).
- This recipe is not tested against the live provider: the tests use a local fake of the OpenAI API. They do not start a Bonsai server or download weights.

## Tests

Run these commands from the repository root. All provider responses are synthetic: in-process MockTransport at both documented URLs, plus a server and OTLP receiver bound to `127.0.0.1`. A subprocess socket guard refuses non-loopback connections before DNS and has a loopback positive control.

Repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/prismml/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/prismml/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Published packages:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/prismml/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/prismml/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

To check the `traceai-openai` SDK floor, change the source command's OpenAI pin to `openai==1.69.0`.

| Python | `openai` | traceAI packages | Result |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |
