# OpenLLMetry (Traceloop SDK) → Future AGI (recipe)

There is no traceAI package for OpenLLMetry. The Traceloop SDK already
exports OTLP/HTTP. This example points it at fi-collector with environment
variables, adds the Future AGI project as a resource attribute, and turns
content capture and metrics off. The backend already maps
`traceloop.span.kind`; this recipe adds no second mapping.

Pinned: `traceloop-sdk` 0.62.4 (`Requires-Python: <4,>=3.10`). Its wheel
METADATA declares `License-Expression: Apache-2.0`; it has no Trove
classifiers and ships no license file. It is a dependency of this example
only, not of any traceAI package. 0.62.4 imports `httpx` and `requests`
without declaring them, so `requirements.txt` lists both.

The contract test (see "Tests") passes on Python 3.10.17, 3.11.12, 3.12.10
and 3.13.15 with `openai` 3.24.0 (pinned in `requirements.txt`),
`opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` 1.45.0 and
`opentelemetry-instrumentation-openai` 0.62.4, the versions uv resolved on
2026-10-05.

## Run

```bash
cd python/examples/openllmetry
pip install -r requirements.txt

export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export TRACELOOP_BASE_URL="https://YOUR_FI_COLLECTOR_ORIGIN"  # no path
export TRACELOOP_HEADERS="$(python -c 'import os; from urllib.parse import quote; print("x-api-key={0},x-secret-key={1}".format(quote(os.environ["FI_API_KEY"], safe=""), quote(os.environ["FI_SECRET_KEY"], safe="")))')"
export TRACELOOP_TRACE_CONTENT=false    # the SDK default is true
export TRACELOOP_METRICS_ENABLED=false  # fi-collector serves no /v1/metrics
export FI_PROJECT_NAME="my-chatbot"     # resource attribute project_name
export OPENAI_API_KEY="YOUR_OPENAI_KEY" # optional: OPENAI_BASE_URL, OPENAI_MODEL
unset TRACELOOP_API_KEY                 # src/app.py refuses to start with it

python src/app.py "What is the refund window?"
```

## The recipe

`init_tracing()` in `src/app.py` is the whole integration:

```python
Traceloop.init(
    app_name="openllmetry-recipe",  # service.name
    resource_attributes={
        "project_name": os.environ["FI_PROJECT_NAME"],
        "project_type": "observe",
    },
    telemetry_enabled=False,
)
```

Everything else comes from the environment above, read by the SDK itself.
fi-collector rejects a batch whose resource has no `project_name` (HTTP 400)
and creates the project on first use. The rest of `src/app.py` is a
`@workflow` that calls a `@task` and an `@agent`; the agent calls a `@tool`
and the OpenAI Chat Completions API.

### Endpoint

Set `TRACELOOP_BASE_URL` to the collector origin with no path. With an
`http://` or `https://` URL the SDK uses OTLP/HTTP and posts to
`<base URL>/v1/traces`, which fi-collector serves. Tested results with
0.62.4:

| `TRACELOOP_BASE_URL` | Request path |
|---|---|
| `http://host:port` | `/v1/traces` |
| `http://host:port/` | `/v1/traces` |
| `http://host:port/v1/traces` | `/v1/traces` (0.62.4 does not append it twice) |
| `http://host:port/tracer` | `/tracer/v1/traces` |

fi-collector also serves `/tracer/v1/traces`, the path traceAI's own
exporter appends to `FI_BASE_URL`. If your collector is only reachable under
that path, set the base URL to `<origin>/tracer`. These paths were tested
against a loopback receiver; no deployed endpoint was called.

A base URL with no scheme makes the SDK switch to gRPC. This recipe does not
test gRPC.

### Auth headers

`TRACELOOP_HEADERS` carries the Future AGI keys. The SDK parses it with
OpenTelemetry's `parse_env_headers`: comma-separated `name=value` pairs, each
value percent-decoded, names lower-cased. The contract test proves that
`x-api-key=<key>,x-secret-key=<secret>`, each value percent-encoded as in the
`export` line above, arrives as the `x-api-key` and `x-secret-key` request
headers that fi-collector reads. HTTP header names are case-insensitive.

Percent-encode the values. The SDK splits on every comma, so an unencoded
comma cuts a value short (tested), and it drops, with only a logged warning,
any pair whose value has a space, `"`, `;` or `\`. That warning prints the
whole pair, value included, so an unencoded secret with one of those
characters ends up in your logs.

Do not set `TRACELOOP_API_KEY`. That key is for Traceloop's own host.
Without `TRACELOOP_HEADERS` the SDK sends it as `Authorization: Bearer` on
every export. With them it is not sent with spans, but the SDK still uses it
for its image uploader (see "Privacy"). `src/app.py` exits if it is set.

### Metrics

The SDK exports metrics by default to `<base URL>/v1/metrics` (tested).
fi-collector routes no `/v1/metrics`, so the recipe sets
`TRACELOOP_METRICS_ENABLED=false`.

### Telemetry

`telemetry_enabled=False` is passed. In 0.62.4 that parameter is accepted
and never read, and the package has no telemetry client. The contract test
runs the app with every non-loopback connection blocked and logged, and no
run logs an attempt.

## What is traced

With content off, `python src/app.py` exports five spans in one trace:

| Span | `traceloop.span.kind` | Parent | Attributes |
|---|---|---|---|
| `support_request.workflow` | `workflow` | none | `traceloop.entity.name`, `traceloop.workflow.name` |
| `normalize_question.task` | `task` | workflow | same |
| `support_agent.agent` | `agent` | workflow | same, plus `gen_ai.agent.name` |
| `lookup_policy.tool` | `tool` | agent | same, plus `gen_ai.agent.name`, `gen_ai.tool.name` |
| `openai.chat` | none | agent | `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens`, `gen_ai.is_streaming`, `gen_ai.openai.api_base`, `gen_ai.agent.name`, `traceloop.workflow.name` |

0.62.4 emits the `gen_ai.*` model and usage keys, not the older
`llm.request.model` or `llm.usage.prompt_tokens`. fi-collector's alias list
reads the `gen_ai.*` keys first.

The backend's OpenLLMetry adapter
(`futureagi/tracer/utils/adapters/openllmetry.py`) maps
`traceloop.span.kind`. Its map, quoted so this page adds no second one:

| `traceloop.span.kind` | Future AGI kind |
|---|---|
| `workflow` | CHAIN |
| `task` | CHAIN |
| `agent` | AGENT |
| `tool` | TOOL |
| `unknown` | UNKNOWN |

A span without `traceloop.span.kind` takes its kind from
`gen_ai.operation.name`: `chat` and `completion` are LLM, `embedding` and
`embeddings` EMBEDDING, `rerank` RERANKER, `execute_tool` TOOL. The opt-in
tests under "Tests" read both maps from that file, check the table above
against the first, and check the emitted span kinds and operation names
against both.

The adapter recognises a span as OpenLLMetry only when it has a
`traceloop.*` key. A model call made inside a decorated function carries
`traceloop.workflow.name`. A model call made outside any Traceloop decorator
has no `traceloop.*` key (tested), so call the model from inside a
`@workflow`.

The agent, tool and model-call spans carry both `gen_ai.*` and
`traceloop.*` keys (tested). That each of them is stored with one kind is
the shared processor's (SF-1) assertion, not this recipe's.

## Privacy

Content capture is on unless `TRACELOOP_TRACE_CONTENT` is set: the SDK
treats an unset variable as `true`. With content on, the model-call span
carries `gen_ai.input.messages` (system prompt and user question, including
anything your code put into the prompt) and `gen_ai.output.messages`, and
every decorated span carries `traceloop.entity.input` and
`traceloop.entity.output` (the function's arguments and return value as
JSON). The contract test finds the question, the answer and the tool output
in those keys with the variable unset, and none of those keys and none of
that text anywhere in the export with it set to `false`.

Use `true` or `false` only. The Traceloop decorators treat only `true` as
on; the OpenAI instrumentor also treats `1`, `yes` and `on` as on.

Not covered by the content switch (read in the 0.62.4 source, not tested):

- When a decorated function raises, its span gets the exception message as
  its status and an `exception` event with the message and stack trace.
- `gen_ai.openai.api_base` (the OpenAI base URL) is on every model-call span.

With content on, a base64 image in a prompt makes the OpenAI instrumentor
upload it to `<base URL>/v2/traces/<trace>/spans/<span>/images` with
`Authorization: Bearer <TRACELOOP_API_KEY>`. fi-collector has no such route.
Keep content off, or keep base64 images out of prompts.

No Future AGI key, secret or OpenAI key appears in any exported span or
resource, or in the app's output (tested).

## Notes

- Traceloop uses a `BatchSpanProcessor` and registers an `atexit` flush, so
  a short script exports on exit and a server exports in batches.
- With content on, 0.62.4 writes messages as `gen_ai.input.messages` and
  `gen_ai.output.messages`. The backend adapter (future-agi `main` 4af5338,
  `futureagi/tracer/utils/adapters/openllmetry.py`) builds the span's input
  and output only from indexed `gen_ai.prompt.<i>.*` and
  `gen_ai.completion.<i>.*` keys (`:114`, `:214-215`), then deletes every
  `gen_ai.*` key (`:231`). Nothing in it reads `gen_ai.input.messages`, and
  the model-call span carries no `traceloop.entity.input`, so with 0.62.4 the
  model call's input and output are dropped by that adapter, not shown. This
  is read from the backend source, not run against a backend; it is a backend
  item (SF-1), and the recipe keeps content off.
- No call to fi-collector, Traceloop or OpenAI was made. Auth rejection
  (401 without keys), project stamping and storage are fi-collector
  behaviour the harness receiver does not reproduce.

## Tests

`tests/test_openllmetry_recipe.py` runs `src/app.py` as written, in a
subprocess, with the real `traceloop-sdk` 0.62.4 instrumenting the real
`openai` client. `tests/_fake_openai.py` is a loopback fake of the Chat
Completions API; the spans go to the shared harness `Receiver`
(`python/tests/harness`), which serves `/v1/traces` and `/tracer/v1/traces`
on 127.0.0.1. `tests/_guarded_run.py` blocks and logs every non-loopback
connection; a positive control (`tests/_guard_probe.py`) proves it refuses
and logs IPv4, IPv6 and DNS attempts. All keys are placeholders.

The tests check the request path, the auth headers, the resource
attributes, the span kinds and tree, content off and on, metrics off and
on, the API key guard, and that no key reaches the export.
`tests/bare_llm_call.py` is a fixture (a model call outside any decorator),
not part of the recipe.

From the repository root, Python 3.11:

```bash
env -u PYTHONPATH PYTHONPATH="python/examples/openllmetry:python:python/tests" \
  uv run --no-project --python 3.11 \
  --with pytest --with pytest-asyncio --with opentelemetry-api \
  --with opentelemetry-sdk --with opentelemetry-exporter-otlp-proto-http \
  --with requests --with jsonschema --with protobuf --with opentelemetry-proto \
  --with 'traceloop-sdk==0.62.4' --with 'openai==3.24.0' --with httpx \
  pytest python/examples/openllmetry/tests -q -p no:cacheprovider \
  --noconftest -o addopts= -rfEs
```

For Python 3.10, 3.12 or 3.13, replace `--python 3.11`. Without
`--with httpx` every run fails at `import traceloop.sdk`.

Two tests are opt-in. They read `_TRACELOOP_KIND_MAP` and
`_OPERATION_KIND_MAP` from the backend's `openllmetry.py` and check the
emitted kinds and the kind table above against them. Point
`FI_BACKEND_OPENLLMETRY_PY` at a copy of that file:

```bash
git -C <future-agi checkout> show origin/main:futureagi/tracer/utils/adapters/openllmetry.py > /tmp/openllmetry.py
export FI_BACKEND_OPENLLMETRY_PY=/tmp/openllmetry.py   # then run the command above
```

Without it they are skipped. No CI job runs this example, so run the pair
whenever the backend adapter or this recipe changes; otherwise the kind table
above can drift from the backend unnoticed. The tests start the app in a new process 10
times, and importing and initialising the SDK and its instrumentors takes
most of each run, so a full run takes about 5 minutes.
