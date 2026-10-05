# MLflow → Future AGI (recipe)

There is no traceAI package for MLflow. MLflow already exports its own
traces over OTLP; this example points that export at fi-collector with
environment variables and adds the Future AGI project as a resource
attribute. It adds no instrumentor: a wrapper around `mlflow.start_span`
would double every span.

This is live export only. Historical import is not this integration: there
is no tracking-server client and no import command, and traces that are
already in an MLflow tracking server, Databricks or Unity Catalog are not
sent. Only spans that end while the variables below are set are exported.

Pinned: `mlflow==3.16.1`, the latest on PyPI on 2026-10-05
(`Requires-Python: >=3.10`). Its wheel METADATA has no
`License-Expression`; its `License` field holds the Apache License 2.0 text
under a Databricks copyright line, it declares
`Classifier: License :: OSI Approved :: Apache Software License`, and it
ships `LICENSE.txt`. It is a dependency of this example only, not of any
traceAI package. `mlflow` 3.16.1 depends on `opentelemetry-sdk` but installs
no OTLP exporter, so `requirements.txt` adds
`opentelemetry-exporter-otlp-proto-http==1.45.0`.

The contract test (see [Tests](#tests)) passes on Python 3.10.17, 3.11.12,
3.12.10 and 3.13.15 with `opentelemetry-api`, `opentelemetry-sdk` and
`opentelemetry-proto` 1.45.0 and `protobuf` 6.33.6, the versions uv resolved
on 2026-10-05. Python 3.14 was not tested. Statements marked "source
reading" come from the mlflow 3.16.1 wheel or from fi-collector at
future-agi `main` 4af5338 and are not exercised by the tests. No MLflow
tracking server, Databricks workspace, model provider or Future AGI endpoint
was called.

## Run

```bash
cd python/examples/mlflow
pip install -r requirements.txt

export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
# The full URL: MLflow does not append /v1/traces to this variable.
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://YOUR_FI_COLLECTOR_ORIGIN/tracer/v1/traces"
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="http/protobuf"   # MLflow's default is gRPC
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="$(python -c 'import os; from urllib.parse import quote; print("x-api-key={0},x-secret-key={1}".format(quote(os.environ["FI_API_KEY"], safe=""), quote(os.environ["FI_SECRET_KEY"], safe="")))')"
export OTEL_RESOURCE_ATTRIBUTES="project_name=my-mlflow-app,project_type=observe"
export OTEL_SERVICE_NAME="my-mlflow-app"   # optional: service.name
export MLFLOW_DISABLE_TELEMETRY=true
unset OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_METRICS_ENDPOINT

python src/app.py "What is the refund window?"
```

Set the variables before the first span starts: MLflow builds its exporter
once, on first use. `src/app.py` is two nested `mlflow.start_span` calls
around a stand-in model client that returns a canned answer, so it makes no
model call. Its `check_environment()` exits with a message on each setting
below that would make MLflow export nothing.

## The recipe

| Variable | Value | Why |
|---|---|---|
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `<collector origin>/tracer/v1/traces` | MLflow uses it as is. |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `http/protobuf` | MLflow defaults to gRPC. |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | `x-api-key=<key>,x-secret-key=<secret>`, values percent-encoded | The headers fi-collector reads. |
| `OTEL_RESOURCE_ATTRIBUTES` | `project_name=<project>,project_type=observe` | fi-collector needs `project_name`. |
| `OTEL_SERVICE_NAME` | optional | Otherwise `service.name` is OpenTelemetry's `unknown_service` default. |
| `MLFLOW_DISABLE_TELEMETRY` | `true` | See [Telemetry](#telemetry). |

### Endpoint

MLflow passes `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` unchanged to the
OpenTelemetry HTTP exporter (`mlflow/tracing/utils/otlp.py:98-99`, `:81`),
so it must be the full traces URL. It does not append `/v1/traces`. Tested
against a loopback receiver with mlflow 3.16.1:

| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Request path | Result |
|---|---|---|
| `http://host:port/tracer/v1/traces` | `/tracer/v1/traces` | 200, the recipe's form |
| `http://host:port/v1/traces` | `/v1/traces` | 200 |
| `http://host:port` | `/` | 404, spans lost |
| `http://host:port/` | `/` | 404, spans lost |
| `http://host:port/tracer` | `/tracer` | 404, spans lost |

fi-collector serves both `/v1/traces` and `/tracer/v1/traces`. The recipe
uses `/tracer/v1/traces` because that is where traceAI's own exporter posts
(`python/fi_instrumentation/otel.py:719`, origin from `FI_BASE_URL`, default
`https://api.futureagi.com`), so use the same origin you give traceAI. A 404
shows only as `Failed to export spans batch code: 404` on stderr; the app
does not fail. With the variable unset, MLflow sends nothing and writes the
trace to `./mlflow.db` instead (tested).

Do not set `OTEL_EXPORTER_OTLP_ENDPOINT` or
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`. MLflow derives the traces URL from the
first (appending `/v1/traces`, `otlp.py:101-102`), but either one also turns
on MLflow's OTLP metrics (`otlp.py:45-51`), whose protocol defaults to gRPC
(`mlflow/tracing/processor/otel_metrics_mixin.py:46-56`). Tested: with
either one set and no gRPC exporter installed, every span fails at
`end()` with `No module named 'opentelemetry.exporter.otlp.proto.grpc'` and
nothing is sent. With `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` as well,
traces arrive and the metrics go to `/v1/metrics`, which fi-collector does
not serve (404).

### Protocol

`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` must be `http/protobuf`. MLflow's
default is `grpc` (`otlp.py:124-136`). The gRPC exporter is not installed
here, so MLflow cannot build its exporter; `mlflow.start_span` then hands
out no-op spans and logs that only at debug level (`fluent.py:660-662`).
Tested: no request, nothing on stderr, exit code 0.

### Auth headers

The OpenTelemetry HTTP exporter (1.45.0) reads
`OTEL_EXPORTER_OTLP_TRACES_HEADERS` itself. It splits the value on commas
and percent-decodes each value (`opentelemetry/util/re.py:60-76` in
`opentelemetry-api`). The test shows that
`x-api-key=<key>,x-secret-key=<secret>`, built as in the `export` line
above, arrives as the `x-api-key` and `x-secret-key` request headers that
fi-collector reads (`pkg/auth/middleware.go:45-46`), with a secret that
contains `,` and `=` intact, and no `authorization` header. An unencoded
comma cuts the secret short (tested). Do not copy the `api_key=12345` header
from MLflow's documentation; it is not a Future AGI credential.

### Resource: project, experiment and session

`OTEL_RESOURCE_ATTRIBUTES` carries `project_name` and `project_type`
(`observe`) on every export (tested). fi-collector rejects a batch in which
a resource has no `project_name` (`pkg/auth/stamp.go:31-44`). Percent-encode
a project name that contains a comma: write `My bot, EU` as
`project_name=My%20bot%2C%20EU` and it arrives decoded (the test uses a
name with a comma and spaces).

MLflow re-parses this variable before OpenTelemetry reads it
(`mlflow/tracing/provider.py:693-697`, `:716-731`). If one item has no `=`,
a trailing comma included, it drops every attribute in the variable, so
`project_name` is lost (tested). `src/app.py` refuses that.

The export also carries `service.name`, `service.instance.id`,
`telemetry.sdk.language`, `telemetry.sdk.name` (`mlflow`) and
`telemetry.sdk.version` (`3.16.1`).

MLflow puts no experiment id on the OTLP export, not even with
`MLFLOW_EXPERIMENT_ID` set (tested). If you want one in Future AGI, add it
to `OTEL_RESOURCE_ATTRIBUTES`, for example `mlflow.experiment_id=42`: it
arrives as a resource attribute (tested). The experiment id is a resource
attribute, not a Future AGI session. Future AGI sessions come from a
`session.id` span attribute (`exporter/clickhouse25exporter/converter.go:44`,
`:507-515`), and MLflow 3.16.1 sets none on the OTLP path; it copies
`session.id` only for Databricks Unity Catalog tables
(`mlflow/tracing/processor/uc_table.py:99-100`, source reading).

### Telemetry

With `MLFLOW_DISABLE_TELEMETRY` unset, importing `mlflow` creates MLflow's
usage-telemetry client and writes an installation id to
`$HOME/.config/mlflow/telemetry.json` (tested). That client sends usage
events to MLflow's telemetry host once an API that records events is used
(`mlflow/telemetry/client.py:177-200`, `mlflow/telemetry/utils.py:144-177`,
source reading); no such request was seen in the tests, which run with every
non-loopback connection blocked and logged. The recipe turns it off.
`DO_NOT_TRACK=true` works too.

## Dual export and `set_destination`

`MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT` is optional. Tested:

| `MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT` | Tracking URI | Result |
|---|---|---|
| unset (default) | unset | OTLP only. No file written. |
| `false` | unset | OTLP only. No file written. |
| `true` | unset | OTLP, and MLflow's default store: it creates `./mlflow.db` (SQLite) in the working directory and writes the trace there. |
| `true` | `MLFLOW_TRACKING_URI=sqlite:////abs/path/traces.db` | OTLP, and that store. |

So dual export with no tracking URI is not OTLP-only: it writes a local
database next to your app. In 3.16.1 the default store is `./mlflow.db`
unless `./mlruns` already holds experiment data, in which case it is that
file store (`mlflow/tracking/_tracking_service/utils.py:32-63`; only the
SQLite case was tested).

Writing to an MLflow store also makes MLflow fetch its model catalog to
price LLM spans, from `https://github.com/mlflow/mlflow/releases/download/model-catalog%2Flatest`
by default (`mlflow/environment_variables.py:1758-1766`,
`mlflow/utils/providers.py:252-297`). The test points
`MLFLOW_MODEL_CATALOG_URI` at a loopback server and sees `GET` requests for
`/catalog/openai.json`. Set `MLFLOW_MODEL_CATALOG_URI=` (empty) to turn the
fetch off. OTLP-only export never fetches it.

`mlflow.tracing.set_destination(...)` takes precedence over the OTLP
variables (`mlflow/tracing/provider.py:825-860`). Tested with
`MlflowExperimentLocation(experiment_id="0")`: not one request of any kind
reaches the OTLP endpoint, and the trace is in `./mlflow.db` instead. With
`MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT=true` as well, the trace goes to both.
Do not call `set_destination` in an app that exports to Future AGI, or turn
dual export on.

## What is exported

`src/app.py` exports one trace with two spans: `answer_question`
(`span_type=CHAIN`, the root) and its child `chat_model`
(`span_type=CHAT_MODEL`). Both have OTel kind `INTERNAL` and status `OK`.
MLflow's OTLP processor is an OpenTelemetry `BatchSpanProcessor`
(`mlflow/tracing/processor/otel.py:20`), and the OpenTelemetry SDK flushes it
at exit (`opentelemetry/sdk/trace/__init__.py:1283-1284`), so a short script
exports when it ends.

MLflow JSON-encodes every attribute value before it sets it on the span
(`mlflow/entities/span.py:1456-1490`, `mlflow/tracing/utils/__init__.py:125-142`).
Every value arrives as an OTLP string (tested): a string arrives with its
quotes, a number as its digits. That applies to keys your code sets too:
MLflow lets the app set `gen_ai.*` keys, but `gen_ai.request.model` arrives
as the 13-character string `"gpt-4o-mini"`, quotes included.

Keys on the default export (tested), and whether fi-collector reads them
(source reading, `pkg/adapter/adapter.go` and
`exporter/clickhouse25exporter/converter.go` at 4af5338):

| Key | Spans | Value as exported | fi-collector |
|---|---|---|---|
| `mlflow.traceRequestId` | both | `"tr-<32 hex>"`, JSON-encoded | no |
| `mlflow.spanType` | both | `"CHAIN"`, `"CHAT_MODEL"`, JSON-encoded | no: kind is read only from `fi.span.kind`, `gen_ai.span.kind`, `llm.request.type`, `openinference.span.kind`, then `gen_ai.operation.name` (`converter.go:79-131`) |
| `mlflow.spanLogLevel` | both | `10` (CHAIN), `20` (CHAT_MODEL), as strings | no |
| `mlflow.spanInputs` | both | what the code passed to `set_inputs`, as JSON | no: the input column reads only `input.value` (`converter.go:343`) |
| `mlflow.spanOutputs` | both | what the code passed to `set_outputs`, as JSON | no: the output column reads only `output.value` (`converter.go:344`) |
| `mlflow.llm.model` | `chat_model` | `"gpt-4o-mini"`, JSON-encoded | no |
| `mlflow.llm.provider` | `chat_model` | `"openai"`, JSON-encoded | no |
| `mlflow.chat.tokenUsage` | `chat_model` | `{"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}` | no |
| `gen_ai.request.model` | `chat_model` | `"gpt-4o-mini"`, JSON-encoded | model, copied as is, quotes included (`adapter.go:281`, `:317-323`) |
| `gen_ai.provider.name` | `chat_model` | `"openai"`, JSON-encoded | provider, copied as is, quotes included (`adapter.go:290`) |
| `gen_ai.usage.input_tokens` | `chat_model` | `12`, as a string | prompt tokens, parsed from the string (`adapter.go:298`, `:339-342`) |
| `gen_ai.usage.output_tokens` | `chat_model` | `7`, as a string | completion tokens, parsed from the string (`adapter.go:303`) |

The `mlflow.llm.*` and `mlflow.chat.tokenUsage` keys are what MLflow's
autolog integrations set on a model call; `src/app.py` sets them by hand,
next to the `gen_ai.*` keys. With these keys, fi-collector would store the
model and provider with their quotes, fill the token columns (total derived
as 19, `adapter.go:232-234`), and store both spans as kind `unknown`
(source reading; no fi-collector was run).

### `MLFLOW_ENABLE_OTEL_GENAI_SEMCONV=true`

MLflow can translate its own keys to OpenTelemetry GenAI keys before OTLP
export (`mlflow/environment_variables.py:1092-1097`,
`mlflow/tracing/processor/otel.py:81-82`,
`mlflow/tracing/export/genai_semconv/translator.py`). It is off by default
and optional here. Tested with it on:

| Key | Spans | Value as exported | fi-collector |
|---|---|---|---|
| `gen_ai.operation.name` | both | `chat`; `CHAIN` on the root (a span type with no GenAI name passes through) | kind when no kind key is set: `chat` becomes llm, `CHAIN` becomes chain (`converter.go:94-131`) |
| `gen_ai.request.model` | chat span | `gpt-4o-mini`, a plain string | model (`adapter.go:281`) |
| `gen_ai.provider.name` | chat span | `openai`, a plain string | provider (`adapter.go:290`) |
| `gen_ai.usage.input_tokens` | chat span | `12`, an integer | prompt tokens (`adapter.go:298`) |
| `gen_ai.usage.output_tokens` | chat span | `7`, an integer | completion tokens (`adapter.go:303`) |
| `gen_ai.system_instructions` | chat span | the system message, as JSON | no |
| `gen_ai.input.messages` | chat span | the other input messages, as JSON | no: the input column reads only `input.value` |
| `gen_ai.output.messages` | chat span | the output messages, as JSON | no: the output column reads only `output.value` |

What else changes (tested):

- Every `mlflow.*` key is dropped, including `mlflow.spanInputs` and
  `mlflow.spanOutputs`. Chat-shaped inputs and outputs become the three
  message keys above (`genai_semconv/converter.py:62-80`); the root span's
  `{"question": ...}` and `{"answer": ...}` are dropped.
- Spans are renamed. The model span becomes `chat gpt-4o-mini`; a span with
  a type but no model is renamed to its type, so `answer_question` becomes
  `CHAIN` (`translator.py:164-175`).
- The model span's OTel kind becomes `CLIENT`.
- The `gen_ai.*` keys the app set itself are overwritten by the translated
  values, which come from `mlflow.llm.model`, `mlflow.llm.provider` and
  `mlflow.chat.tokenUsage`. A manual span that sets only `gen_ai.*` keys
  keeps their JSON-encoded values.

How MLflow's span types would be typed in Future AGI with the switch on
(source reading: the operation name from `translator.py:26-32`, then
fi-collector lower-cases it and applies `spanKindSynonyms` and
`knownObservationTypes`, `converter.go:62-131`). With the switch off every
MLflow span is `unknown`.

| MLflow span type | `gen_ai.operation.name` | Future AGI kind |
|---|---|---|
| `CHAT_MODEL` | `chat` | llm |
| `LLM` | `generate_content` | llm |
| `EMBEDDING` | `embeddings` | embedding |
| `TOOL` | `execute_tool` | tool |
| `AGENT` | `invoke_agent` | unknown |
| `CHAIN` | `CHAIN` | chain |
| `RETRIEVER` | `RETRIEVER` | retriever |
| `RERANKER` | `RERANKER` | reranker |
| `GUARDRAIL` | `GUARDRAIL` | guardrail |
| `EVALUATOR` | `EVALUATOR` | evaluator |
| `PARSER` | `PARSER` | unknown |
| `MEMORY` | `MEMORY` | unknown |
| `WORKFLOW` | `WORKFLOW` | unknown |
| `TASK` | `TASK` | unknown |
| `UNKNOWN` | `UNKNOWN` | unknown |

The switch is the only way found to get a model name without quotes and
typed spans out of MLflow 3.16.1 without code. Its cost is the renamed
spans and the dropped non-chat inputs and outputs. That trade-off is yours;
the recipe leaves it off.

### Gaps for the shared processor (SF-1)

Nothing in this example changes fi-collector or adds an alias. These are the
MLflow keys fi-collector does not map, read from the source above, for the
shared processor work:

- `mlflow.spanType`: raw. Proposed SF-1 alias for the span kind, after JSON
  decoding (types as in the table above; `AGENT` to agent).
- `mlflow.llm.model`, `mlflow.llm.provider`: raw. Proposed SF-1 model and
  provider aliases, after JSON decoding.
- `mlflow.chat.tokenUsage`: raw. Proposed SF-1 token source: its
  `input_tokens`, `output_tokens` and `total_tokens` fields.
- `mlflow.spanInputs`, `mlflow.spanOutputs`: raw. Proposed SF-1 input and
  output source.
- JSON-encoded string values on `gen_ai.*` keys from MLflow spans (model and
  provider with quotes): proposed SF-1 decoding for spans whose resource has
  `telemetry.sdk.name=mlflow`.
- `gen_ai.operation.name=invoke_agent`: proposed SF-1 synonym for agent.
- `gen_ai.system_instructions`, `gen_ai.input.messages`,
  `gen_ai.output.messages`: raw; not lifted into the input and output
  columns.
- `mlflow.traceRequestId`, `mlflow.spanLogLevel`: raw; no Future AGI column.

Whether fi-collector promotes any of these is not asserted here: the
harness receiver decodes and records, it does not run fi-collector.

## Content

MLflow has no setting that keeps tracing on and content off. For
`mlflow.start_span`, MLflow records what your code passes to `set_inputs`,
`set_outputs` and `set_attribute(s)`. In the test, the question, the system
prompt (which holds the policy text) and the answer are in
`mlflow.spanInputs` and `mlflow.spanOutputs`, and in no other key. With
`MLFLOW_ENABLE_OTEL_GENAI_SEMCONV=true` they are in
`gen_ai.system_instructions`, `gen_ai.input.messages` and
`gen_ai.output.messages` instead (tested). `@mlflow.trace` and MLflow's
autolog integrations record function arguments, return values and model
messages without being asked (source reading; not tested here). Whatever
MLflow captures lands in Future AGI.

MLflow's own hook for removing it is a span processor:

```python
def drop_content(span):
    if span.inputs is not None:
        span.set_inputs("[REDACTED]")
    if span.outputs is not None:
        span.set_outputs("[REDACTED]")

mlflow.tracing.configure(span_processors=[drop_content])
```

MLflow runs it when a span ends, before export (`mlflow/entities/span.py:1176-1177`).
`python src/app.py --drop-content ...` registers it. Tested with the GenAI
switch off and on: none of the question, the policy text or the answer is
anywhere in the export; with the switch off the two keys remain, holding
`"[REDACTED]"`, and with it on no message key is exported.

Not covered by that processor (source reading, not tested): when code
inside a span raises, MLflow sets the span status to `ERROR` and adds an
`exception` event with the message and stack trace
(`mlflow/tracing/provider.py:309-311`, `mlflow/entities/span.py:1062-1073`),
and attributes your code sets with other keys are exported as they are.

No Future AGI key or secret appears in any exported span or resource, or in
the app's output (tested); they travel only as request headers.

## Troubleshooting

- **No request reaches the collector, no error.** Check
  `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf` and that
  `opentelemetry-exporter-otlp-proto-http` is installed. Then check for a
  `mlflow.tracing.set_destination(...)` call: it wins over the OTLP
  variables.
- **`Failed to end span ...: No module named 'opentelemetry.exporter.otlp.proto.grpc'`.**
  `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` is
  set. Unset both.
- **`Failed to export spans batch code: 404`.** The traces URL has no
  `/tracer/v1/traces` (or `/v1/traces`) path. MLflow does not add it.
- **HTTP 400 from the collector, or no project.** `project_name` is missing
  from `OTEL_RESOURCE_ATTRIBUTES`, or one item in it has no `=` (a trailing
  comma), which makes MLflow drop them all.
- **An `mlflow.db` file appeared.** `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is
  unset, or dual export or `set_destination` is writing to MLflow's default
  local store.
- **Model name shown with quotes.** That is MLflow's JSON encoding; see
  [What is exported](#what-is-exported).
- **Old traces never arrived.** Expected: this is live export, not a
  backfill.

## Tests

`tests/test_mlflow_recipe.py` runs `src/app.py` as written, in a subprocess,
with the real `mlflow` 3.16.1. Each run gets a fresh temporary directory as
its working directory and `HOME`, so every file MLflow writes is checked.
Spans go to the shared harness `Receiver` (`python/tests/harness`), which
serves `/v1/traces` and `/tracer/v1/traces` on 127.0.0.1 like fi-collector
but does not authenticate, stamp projects or store anything. Tests that need
every request path, including 404s and the model catalog, use a loopback
catch-all instead. `tests/_guarded_run.py` blocks and logs every
non-loopback connection and every test asserts its log is empty; a positive
control (`tests/_guard_probe.py`) proves it refuses and logs IPv4, IPv6 and
DNS attempts. All keys are placeholders.

`tests/bare_span.py` (the app's spans without `check_environment()`) and
`tests/set_destination_app.py` (the app after a `set_destination` call) are
fixtures, not part of the recipe. `tests/fixtures/gen_ai_span.otlp.json` is
a hand-built OTLP request with two `gen_ai.*` spans, one in the shape MLflow
sends with the GenAI switch on and one in the default JSON-encoded shape.
The test posts it with the harness `post_otlp()` and checks that it arrives
with `gen_ai.request.model` and `gen_ai.usage.input_tokens` intact, and
checks each fixture value against a live MLflow run.

From the repository root (Python 3.12 shown):

```bash
env -u PYTHONPATH PYTHONPATH="python:python/tests" \
  uv run --no-project --python 3.12 \
  --with pytest --with protobuf --with opentelemetry-proto \
  --with wrapt --with jsonschema \
  --with 'mlflow==3.16.1' --with 'opentelemetry-exporter-otlp-proto-http==1.45.0' \
  pytest python/examples/mlflow/tests -q -p no:cacheprovider \
  --noconftest -o addopts= -rfEs
```

For Python 3.10, 3.11 or 3.13, replace `--python 3.12`. `wrapt` and
`jsonschema` are not used by the recipe: pytest imports the repository's
`python/__init__.py` (traceAI) while collecting, and that import needs them.

Three tests are opt-in. They read fi-collector's alias tables from its
source, check that the `gen_ai.*` keys above are in them and no `mlflow.*`
key is, and check the "fi-collector" column of the key tables, and the span
type table (against MLflow's installed `translator.py` too), against them.
Point `FI_COLLECTOR_SRC` at a fi-collector checkout (the directory that
holds `pkg/` and `exporter/`):

```bash
export FI_COLLECTOR_SRC=<future-agi checkout>/fi-collector   # then run the command above
```

Without it they are skipped. No CI job runs this example, so run them
whenever fi-collector's aliases or this recipe change. The tests start
MLflow in a new process about 30 times, so a full run takes 1 to 2 minutes.
