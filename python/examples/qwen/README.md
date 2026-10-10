# Qwen (OpenAI-compatible) with traceAI

This recipe traces Qwen Chat Completions through the official `openai` Python SDK at `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`. It uses the existing `traceai-openai` instrumentor; there is no Qwen tracing package.

## Install

From `python/examples/qwen/`, install the tested pins:

```bash
pip install -r requirements.txt
```

The unpinned form is:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

## Configure

| Variable | Required | Purpose |
| --- | --- | --- |
| `FI_API_KEY` | Yes | Future AGI API key. Goes to the tracer only. |
| `FI_SECRET_KEY` | Yes | Future AGI secret key. Goes to the tracer only. |
| `DASHSCOPE_API_KEY` | Yes | Model Studio key for the selected region. Goes to the OpenAI client only. |
| `DASHSCOPE_MODEL` | Yes, unless `--model` is supplied | A current model id from Model Studio. The example uses `qwen-plus`. |
| `DASHSCOPE_BASE_URL` | No | Your console workspace's OpenAI-compatible SDK base URL. Overrides the example default. |
| `FI_BASE_URL` | No | Future AGI collector origin. Defaults to `https://api.futureagi.com`; the tracer adds `/tracer/v1/traces`. |

The default is the legacy international (Singapore) host. Alibaba asks you to migrate to your workspace host. Your console workspace host wins when you have one: set `DASHSCOPE_BASE_URL` to that URL. Copy the base for your key's region from the console.

## Run

Run from `python/examples/qwen/`. Replace the placeholder keys before a live call.

```bash
export FI_API_KEY="placeholder-futureagi-key"
export FI_SECRET_KEY="placeholder-futureagi-secret"
export DASHSCOPE_API_KEY="placeholder-qwen-key"
export DASHSCOPE_MODEL="qwen-plus"
# Optional: export DASHSCOPE_BASE_URL to your console workspace's SDK base URL.
python src/app.py
python src/app.py --stream
```

Use `--prompt "What is a rainbow?"` to change the prompt. `--model` overrides `DASHSCOPE_MODEL`. Streaming requests set `stream_options={"include_usage": True}`. An unresolved workspace placeholder or a wrong Alibaba SDK path returns exit code 2 before tracing or a network call.

## Code

Instrument before creating the client. The runnable [src/app.py](src/app.py) also validates the base URL and handles streaming.

```python
import os
from openai import OpenAI
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
provider = register(
    project_name="qwen-openai-recipe", project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=False, verbose=False,
)
OpenAIInstrumentor().instrument(tracer_provider=provider)
base_url = os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
client = OpenAI(base_url=base_url, api_key=os.environ["DASHSCOPE_API_KEY"])
response = client.chat.completions.create(model=os.environ["DASHSCOPE_MODEL"], messages=[{"role": "user", "content": "What is a rainbow?"}])
print(response.choices[0].message.content)
provider.force_flush()
```

## What you see in Future AGI

The tested call exports one `ChatCompletion` span with `gen_ai.span.kind=LLM`. On normal, non-streamed calls, `gen_ai.request.model` is the model id the provider returns in its response. The provider field says `openai`: `gen_ai.provider.name` is the shared instrumentor's label, even when the request goes to Qwen.

Streamed calls record accumulated output text in `output.value`, but the span for a stream has no `gen_ai.request.model` attribute; the model you requested is still inside the `gen_ai.request.parameters` JSON (unless `FI_HIDE_LLM_INVOCATION_PARAMETERS=true`). With `stream_options={"include_usage": True}`, the local fixture supplies a final chunk with `usage` and no choices; the span records input, output, and total token counts of 5, 7, and 12. Without `include_usage`, the fixture supplies no usage and the span has no `gen_ai.usage.*` attributes. Missing usage attributes are omitted, never set to zero.

Failed calls produce an error span with no `gen_ai.request.model` attribute (the requested model is again inside `gen_ai.request.parameters`) and include an exception event. The cross-region error fixture raises `openai.AuthenticationError` and exports an ERROR span. This is current `traceai-openai` behaviour, pinned by the tests. The observations use a local fake rather than the live provider.

## Provider specifics

Model Studio documents these SDK bases. `{WorkspaceId}` is a placeholder: replace it with your workspace id from the console.

| Region | OpenAI SDK base URL |
| --- | --- |
| Beijing | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |
| Virginia | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` |
| Singapore | `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` |
| Japan (Tokyo) | `https://{WorkspaceId}.ap-northeast-1.maas.aliyuncs.com/compatible-mode/v1` |

Migration: Beijing moves from `https://dashscope.aliyuncs.com` to `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com`; Singapore moves from `https://dashscope-intl.aliyuncs.com` to `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com`; Hong Kong moves from `https://cn-hongkong.dashscope.aliyuncs.com` to `https://{WorkspaceId}.cn-hongkong.maas.aliyuncs.com`. Append `/compatible-mode/v1` when using these origins as an OpenAI SDK base. Alibaba recommends the workspace hosts; the legacy hosts are still accepted, which is why the worked example can use the legacy international host.

The key and the region must match; a key from another region gets HTTP 401 `invalid_api_key` with the message "Incorrect API key provided". This 401 comes from Model Studio on the provider request: it is a region mismatch, not a Future AGI authentication failure. Your `FI_API_KEY` and `FI_SECRET_KEY` are not involved, and the trace still shows the call as an ERROR span. Select a current model name from Model Studio rather than assuming `qwen-plus` is available in every region.

`check_base_url` rejects literal `{WorkspaceId}` and URL-encoded `%7BWorkspaceId%7D` placeholders anywhere in the URL. An `aliyuncs.com` URL must end its path with `/compatible-mode/v1`, with one optional trailing slash. Allowed URLs are returned unchanged. The OpenAI SDK itself adds a trailing slash to `client.base_url`; it joins the default to `https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions`.

Provider facts were supplied from the [Model Studio compatibility documentation](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope), re-checked on 2026-10-06.

## Privacy

Set `FI_HIDE_INPUTS=true` to keep prompt text out of the exported span. Set `FI_HIDE_OUTPUTS=true` to keep response text out. These optional variables go to the tracer, and the tests include visible controls for both settings. You can also pass `config=TraceConfig(hide_inputs=True)` to `OpenAIInstrumentor().instrument(...)`, importing `TraceConfig` from `fi_instrumentation`.

The provider still receives the prompt. Future AGI masking does not change the provider's own logging. The vendor key stays on the OpenAI client; the tests assert it is absent from span attributes, events, status, resources, and OTLP request headers.

## Limits / not covered

- Qwen-Audio does not support the OpenAI-compatible protocol and is not covered; for Qwen-Audio, use the DashScope protocol (not traced by this recipe).
- The DashScope native protocol/SDK is not covered.
- The Responses API on the same prefix is not covered.
- This recipe is not tested against the live provider (no paid call); the tests use a local fake of the OpenAI API.
- Model availability, workspace access, and real region-key validation must be checked in your Model Studio console.

## Tests

From the repository root, run this exact command:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/qwen/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/qwen/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

The suite also passes on the `traceai-openai` SDK floor: replace `openai==3.24.0` with `openai==1.69.0`. All provider requests use `httpx.MockTransport` or a server on `127.0.0.1`. Subprocesses install a socket/DNS guard before imports. Negative controls prove it rejects the real provider host before DNS or a connection.

Subprocesses derive their package paths from the loaded `fi_instrumentation` and `traceai_openai` packages. They support both worktree source and installed wheels without assuming the repository's package paths. Streaming and error tests assert the model attribute is absent, so a future instrumentor change will visibly change the test results.

| Python | `openai` | traceAI packages | Result |
| --- | --- | --- | --- |
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |

For the floor row, change `openai==3.24.0` to `openai==1.69.0` in the command above.
For the published-package row, keep only the recipe source and the test harness on `PYTHONPATH`
(any repository path would shadow the installed packages):

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/qwen/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with httpx --with protobuf --with opentelemetry-proto --with pytest \
  pytest python/examples/qwen/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```
