# LangSmith → Future AGI (live trace forwarding recipe)

There is no traceAI package for LangSmith, and this example adds none. With
its OpenTelemetry mode on, the LangSmith SDK turns every run into an
OpenTelemetry span. This recipe sends those spans to Future AGI live, while
the app runs.

It is live forwarding only. It does not import historical LangSmith runs or
projects, it has no LangSmith REST client, and it puts no interceptor, proxy,
DNS override or certificate in front of api.smith.langchain.com.

Pinned: `langsmith[otel]==0.14.4` (`Requires-Python: >=3.10`). Its wheel
METADATA declares `License: MIT`; it has no Trove classifiers and the wheel
ships no license file. The `otel` extra installs the OpenTelemetry
SDK and OTLP/HTTP exporter; `requirements.txt` pins both to 1.45.0, the
versions the tests ran with. These are dependencies of this example only, not
of any traceAI package.

## What was tested, and what was not

- **The SDK half was tested in a deliberately narrow app scope.** It runs the
  real langsmith 0.14.4 SDK in OTEL-only mode with this recipe's
  `@traceable` chain/tool/LLM stand-in and no LangSmith key. The pinned
  OTLP/HTTP export path reached a loopback receiver and matched the hand-built
  fixture exactly. The test guard covers Python's `connect`, `connect_ex` and
  `getaddrinfo` paths; it is not an OS-, native-, UDP-, or every-DNS-isolation
  guarantee. Python 3.10.17, 3.11.12, 3.12.10 and 3.13.15.
- **The SDK coverage does not generalize beyond that app scope.** It does not
  test runs produced by LangChain or LangGraph callbacks, streaming, error
  runs, other run types beyond the source maps below, any LangSmith SDK
  version other than 0.14.4, or a real Future AGI collector, storage,
  authentication or rendering path.
- **The collector half was tested against a stand-in.** The fixture was
  posted to the shared harness receiver, not to a real fi-collector. What
  fi-collector derives from it (span type, model, provider, tokens) was read
  from fi-collector's source by opt-in tests, not observed in a running
  fi-collector.
- **The LangSmith side of fan-out was not tested.** In fan-out (hybrid) mode
  the SDK also sends every run to LangSmith's REST API. In the tests those
  calls went to a loopback stub that answers 200 to everything. No LangSmith
  key was used (a key is a spend hard stop), so whether LangSmith accepts the
  runs is not shown here.

No call to LangSmith, Future AGI or a model was made.

## Two modes

| Goal | LangSmith variables | LangSmith key | What langsmith 0.14.4 does |
|---|---|---|---|
| Future AGI only | `LANGSMITH_TRACING=true`, `LANGSMITH_OTEL_ENABLED=true`, `LANGSMITH_OTEL_ONLY=true` | not needed | Exports OpenTelemetry spans only. Makes no call to LangSmith (tested). |
| Fan-out: LangSmith and Future AGI | `LANGSMITH_TRACING=true`, `LANGSMITH_OTEL_ENABLED=true` | `LANGSMITH_API_KEY`, for the LangSmith copy | Calls LangSmith's REST API (`GET /info` once, then `POST /runs/multipart` with the runs) and exports the same runs as OpenTelemetry spans to Future AGI (both seen against loopback stand-ins only). |

The fan-out row's two variables are the ones LangSmith's OpenTelemetry page
names. In 0.14.4 the SDK calls the three modes `langsmith`, `otel` and
`hybrid`, set by `LANGSMITH_TRACING_MODE`. When that variable is unset it
reads `LANGSMITH_OTEL_ONLY=true` as `otel` and `LANGSMITH_OTEL_ENABLED=true`
as `hybrid`; if both kinds are set, `LANGSMITH_TRACING_MODE` wins and the SDK
warns (`langsmith/client.py:240-306`). `LANGSMITH_TRACING_MODE=otel` behaves
like the first row (tested). `LANGSMITH_OTEL_ONLY` is not on LangSmith's page;
it exists in 0.14.4, and the tests show that it stops the REST calls.

`LANGSMITH_TRACING=true` is the default environment on switch in both modes.
Without an override, anything other than exactly `true` traces and exports
nothing (tested); the OTEL variables only choose where traces go.
`LANGSMITH_TRACING_V2`, when present, takes precedence; otherwise
`LANGCHAIN_TRACING_V2`, when present, takes precedence. Each must be exactly
`true` and overrides `LANGSMITH_TRACING`. `langsmith.configure(enabled=False)`
and `tracing_context(enabled=False)` override the environment switches too.

Without a key the SDK only warns, and not at all in `otel` mode
(`client.py:734-760`). In `otel` mode it also skips the `GET /info` call
(`client.py:1918-1921`) and hands each batch only to the OpenTelemetry
exporter (`_internal/_background_thread.py:640-661`).

## Run

```bash
cd python/examples/langsmith
pip install -r requirements.txt

export LANGSMITH_TRACING=true
export LANGSMITH_OTEL_ENABLED=true
export LANGSMITH_OTEL_ONLY=true   # Future AGI only; remove it to fan out (needs LANGSMITH_API_KEY)

export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export FI_PROJECT_NAME="my-langsmith-app"                            # resource attribute project_name
# Clear inherited trace-specific overrides before setting the general FI values.
unset OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
unset OTEL_EXPORTER_OTLP_TRACES_HEADERS
export OTEL_EXPORTER_OTLP_ENDPOINT="https://YOUR_FI_COLLECTOR_ORIGIN"  # no path
export OTEL_EXPORTER_OTLP_HEADERS="$(python -c 'import os; from urllib.parse import quote; print("x-api-key={0},x-secret-key={1}".format(quote(os.environ["FI_API_KEY"], safe=""), quote(os.environ["FI_SECRET_KEY"], safe="")))')"

python src/app.py "What is the refund window?"
```

## The recipe

`init_tracing()` in `src/app.py` is the whole integration:

```python
provider = TracerProvider(
    resource=Resource.create(
        {
            "project_name": os.environ["FI_PROJECT_NAME"],
            "project_type": "observe",
        }
    )
)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
```

It is standard OpenTelemetry: the exporter reads its endpoint and headers
from the `OTEL_EXPORTER_OTLP_*` variables above. The rest of `src/app.py` is
an ordinary `@traceable` app: a chain run that calls a tool run and an LLM
run. The LLM run is a stand-in that returns what a chat model run returns;
put your model call there.

These things about it are load-bearing:

- **Install the provider before the first traced call.** When the LangSmith
  client starts in `otel` or `hybrid` mode it uses the global tracer provider
  if one is set; otherwise it installs its own (`client.py:1390-1418`).
  `src/app.py` calls `init_tracing()` before it creates the client.
- **LangSmith's own provider cannot feed fi-collector.** Its resource is only
  `service.name` (`OTEL_SERVICE_NAME`, default `langsmith`) and
  `langsmith.internal_provider=true`. It is built without reading
  `OTEL_RESOURCE_ATTRIBUTES` (`_internal/otel/_otel_client.py:124-131`),
  so `project_name` cannot be added by environment (tested).
  fi-collector rejects a batch whose resource has no `project_name` with
  HTTP 400 (`fi-collector/pkg/auth/stamp.go:31-46`, `pkg/server/server.go:461-465`).
- **LangSmith's own provider sends your LangSmith key.** Unless
  `OTEL_EXPORTER_OTLP_HEADERS` is set, it sends `x-api-key: <LANGSMITH_API_KEY>`
  and `Langsmith-Project: <project>` to whatever OTLP endpoint is configured
  (`_otel_client.py:116-122`; tested with no key set, so the header was
  empty). fi-collector reads `X-Api-Key` as the Future AGI key
  (`pkg/auth/middleware.go:45-46`). The recipe's provider sends only the
  headers you set.
- **Flush before a short script exits.** `main()` calls `client.flush()`,
  which hands LangSmith's queued runs to the provider, then
  `provider.shutdown()`, which exports them.

Another fan-out shape is an OpenTelemetry Collector that receives the app's
OTLP once and forwards it to both backends; it can add `project_name` with a
resource processor. This recipe does not build or test one.

### Endpoint and headers

`OTEL_EXPORTER_OTLP_ENDPOINT` is the collector origin with no path; the
exporter appends `/v1/traces` (tested). The signal-specific
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` takes precedence and is a full URL used
as given: `<origin>/tracer/v1/traces` posts there (tested). fi-collector
serves both paths (`pkg/server/server.go:233-234`).

`OTEL_EXPORTER_OTLP_TRACES_HEADERS` likewise takes precedence over the
general `OTEL_EXPORTER_OTLP_HEADERS`. The Run block clears both
signal-specific variables before setting the general Future AGI endpoint and
headers. Without those resets, a stale trace endpoint can send the new Future
AGI credentials to another vendor, while stale trace headers can send another
vendor's credential to Future AGI. If you intentionally use signal-specific
settings, set both the full Future AGI trace endpoint and Future AGI headers
together; they override the general values.

The exporter percent-decodes each header value. The test's secret contains
`,` and `=`; percent-encoded as in the `export` line above, it arrives intact
as `x-secret-key` (tested).

## What is traced

`python src/app.py` exports three spans in one trace, each with OTel kind
INTERNAL, status OK and no events:

| Span | Parent | `langsmith.span.kind` | `gen_ai.operation.name` | Future AGI type |
|---|---|---|---|---|
| `support_request` | none | `chain` | `chain` | chain |
| `lookup_policy` | `support_request` | `tool` | `execute_tool` | tool |
| `chat_model` | `support_request` | `llm` | `chat` | llm |

Attributes, from the 0.14.4 exporter (`_internal/otel/_otel_exporter.py:548-627`, `:721-848`)
and checked against the real export:

- Every span: `langsmith.span.kind` (the run type), `langsmith.trace.name`,
  `langsmith.trace.session_name` (the LangSmith project, `default` unless
  `LANGSMITH_PROJECT` is set), `gen_ai.operation.name`, `gen_ai.system`,
  `langsmith.metadata.*` (see [Privacy](#privacy)), `gen_ai.prompt` (the run's
  inputs as JSON) and `gen_ai.completion` (its outputs as JSON).
- Tool span: `gen_ai.tool.name`.
- LLM span: `gen_ai.request.model` (from the run's `ls_model_name` metadata),
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and
  `gen_ai.usage.total_tokens` (from `usage_metadata` in its outputs),
  `gen_ai.response.finish_reasons`, `gen_ai.serialized.name`.

What that means in Future AGI (read from fi-collector source at future-agi
`main` 4af5338, not run):

- **Type.** LangSmith sets none of `fi.span.kind`, `gen_ai.span.kind`,
  `llm.request.type` and `openinference.span.kind`, so fi-collector takes the
  type from `gen_ai.operation.name` (`exporter/clickhouse25exporter/converter.go:79-131`).
  It does not read `langsmith.span.kind`.
- **Model, provider, tokens.** `gen_ai.request.model`, `gen_ai.system` and
  `gen_ai.usage.*` are on fi-collector's alias lists (`pkg/adapter/adapter.go:279-310`),
  so the model, provider and token columns fill on the LLM span. LangSmith
  writes `gen_ai.system=langchain` on runs with no model name, or one it does
  not recognise (`_otel_exporter.py:629-675`), so chain and tool spans show
  provider `langchain`.

How each LangSmith run type is typed. The middle column is LangSmith's map
(`_otel_exporter.py:117-127`); the last is fi-collector's result:

| LangSmith `run_type` | `gen_ai.operation.name` | Future AGI type |
|---|---|---|
| `tool` | `execute_tool` | tool |
| `chain` | `chain` | chain |
| `llm` | `chat` | llm |
| `retriever` | `embeddings` | embedding |
| `embedding` | `embeddings` | embedding |
| `prompt` | `chat` | llm |
| `parser` | `parser` | unknown |

So a retriever run is shown as an embedding and a prompt run as an LLM call.
Only the chain, tool and LLM rows were exported by the tests; the other rows
come from the two maps.

`gen_ai.prompt` and `gen_ai.completion` are single JSON strings, not the
indexed `gen_ai.prompt.<i>.*` keys that the backend's OTel GenAI adapter turns
into input and output messages (`futureagi/tracer/utils/adapters/otel_genai.py:56-57`,
`:135-136`). Whether Future AGI renders them as the span's input and output
was not tested.

## Privacy

**Content is exported by default.** Every span carries its run's inputs in
`gen_ai.prompt` and its outputs in `gen_ai.completion`. For the LLM span that
is the whole message list, system prompt included, and the model's answer
(tested). In fan-out mode LangSmith receives the same content.

`LANGSMITH_HIDE_INPUTS=true` and `LANGSMITH_HIDE_OUTPUTS=true` control those
input and output copies: together, every tested span's `gen_ai.prompt` and
`gen_ai.completion` is `{}`. They do not scrub metadata. The switches also
apply to the LangSmith copy (source reading: `client.py:2468-2475` runs before
either leg). The token counts come from the outputs, so when outputs are
hidden the LLM span has no `gen_ai.usage.*` and no
`gen_ai.response.finish_reasons`,
and the token columns stay empty. The model name stays, and
`langsmith.metadata.usage_metadata` still carries the counts as a JSON string
that fi-collector does not promote (tested). The `hide_inputs`,
`hide_outputs` and `anonymizer` arguments of `langsmith.Client(...)` accept a
function instead (`client.py:2958-2978`; not tested).

Inputs can include `extra_headers`, `query` and `body` data (for example,
`extra_query` and `extra_body`); those values can survive in the raw
`gen_ai.prompt` JSON. A dict-valued
`extra_headers` is not guaranteed to become a valid separate OpenTelemetry
attribute, so do not assume headers land in a distinct attribute. Treat the
raw input copy as sensitive unless you have applied the input-hiding control.

**Metadata.** Every span also carries, as `langsmith.metadata.<NAME>`:

- the run's metadata (`ls_method`, and on the LLM span `ls_provider`,
  `ls_model_name` and `usage_metadata`);
- every `LANGSMITH_*` and `LANGCHAIN_*` environment variable, except names
  containing `key`, `secret`, `token`, `password`, `passwd`, `pwd`,
  `credential` or `email` and a few fixed names
  (`env/_runtime_env.py:172-213`; the tests see `LANGSMITH_TRACING`,
  `LANGSMITH_OTEL_ENABLED` and `LANGSMITH_OTEL_ONLY`);
- `revision_id`: `LANGCHAIN_REVISION_ID` if set, otherwise
  `git describe --tags --always --dirty` of the working directory
  (`env/_runtime_env.py:207-222`).

That filter checks names, not values. For example, `LANGSMITH_ENDPOINT`,
`LANGSMITH_PROJECT` and other allowed variables can be exported as metadata;
a credential-bearing endpoint URL can therefore be exported. Review the values of allowed
LangSmith/LangChain variables rather than treating the name filter as a
privacy boundary.

`LANGSMITH_HIDE_METADATA=true` drops all of it, and with it `ls_model_name`,
so the model column empties too (source reading: `client.py:2480-2486`,
`:2513-2519`, `_otel_exporter.py:516-522`).

The tests use placeholder Future AGI credentials and set no LangSmith key.

## Not included

- No historical import. There is no LangSmith REST client, no project
  backfill and no command that takes a LangSmith project. Future AGI's
  Langfuse import page is not a template for this one.
- No interceptor, proxy, DNS override or certificate in front of
  api.smith.langchain.com.
- No traceAI package, no fi-collector change and no span-kind mapping:
  fi-collector's fallback already types chain, tool and LLM runs.

## Troubleshooting

- **No traces and no error.** `LANGSMITH_TRACING` is not exactly `true`;
  `True` and `1` do not count (`utils.py:121-142`). Check
  `LANGSMITH_TRACING_V2` first, then `LANGCHAIN_TRACING_V2`: when either is
  present it overrides `LANGSMITH_TRACING` and must be exactly `true`. Also
  check for `langsmith.configure(enabled=False)` or
  `tracing_context(enabled=False)`. The OTEL variables alone trace nothing.
- **fi-collector answers 400, "no project_name".** The spans went through
  LangSmith's own provider: `init_tracing()` ran after the LangSmith client
  was created, or not at all.
- **Spans go to LangSmith's OTLP endpoint.** LangSmith's own provider, with
  no `OTEL_EXPORTER_OTLP_ENDPOINT`, exports to
  `<LANGSMITH_ENDPOINT or https://api.smith.langchain.com>/otel/v1/traces`
  (`_otel_client.py:95-104`; source reading). Same fix as above.
- **A LangSmith key is used although `LANGSMITH_API_KEY` is unset.** A
  LangSmith CLI profile in `~/.langsmith/config.json` (or `LANGSMITH_CONFIG_FILE`)
  can supply a key and an endpoint (`_internal/_profiles.py:71-129`,
  `client.py:1286-1299`; source reading).
- **Empty token columns.** `LANGSMITH_HIDE_OUTPUTS=true` is set, or the LLM
  run's outputs have no `usage_metadata`.
- **Retriever runs shown as embeddings.** LangSmith exports `retriever` runs
  with `gen_ai.operation.name=embeddings`; see the run-type table.
- **Spans doubled.** Another instrumentor on the same provider (for example a
  traceAI LangChain instrumentor) records the same calls again. Use one or the
  other (not tested).

## Tests

`tests/test_langsmith_recipe.py` tests the two halves separately.

- **Collector half.** `tests/fixtures/langsmith-0.14.4-run-tree.otlp.json` is
  an OTLP/JSON body written by hand from the 0.14.4 exporter source: a
  `support_request` chain run with a `lookup_policy` tool child and a
  `chat_model` LLM child, with `project_name` and `project_type=observe` on
  the resource. The tests post it with the harness `post_otlp()`, check it
  with `compare()`, and assert one trace, the tool and LLM children, and the
  model, provider and token keys. These need no LangSmith install.
- **SDK half.** `src/app.py` runs as written, in a subprocess, with the real
  langsmith 0.14.4. `tests/_guarded_run.py` intercepts Python
  `socket.connect`, `socket.connect_ex` and `socket.getaddrinfo` paths; its
  positive control (`tests/_guard_probe.py`) exercises those paths for IPv4,
  IPv6 and DNS attempts. It is not a guarantee of OS-, native-, UDP-, or every
  DNS-path isolation. The pinned OTLP/HTTP export path is actually exercised:
  spans go to the shared harness `Receiver` (`python/tests/harness`), which
  serves `/v1/traces` and `/tracer/v1/traces` on 127.0.0.1. The recipe run
  leaves `LANGSMITH_ENDPOINT` unset, so a LangSmith call on the tested Python
  paths would be refused; none occurred. The export must equal the fixture
  (names, attributes and values, statuses). Other runs check the request path,
  headers and resource, the three modes against a loopback LangSmith stub,
  tracing off, the hide switches, and LangSmith's own provider
  (`tests/sdk_own_provider.py`, a fixture, not part of the recipe).

Every child environment is built from scratch with a throwaway `HOME`, so no
LangSmith key or profile is inherited, and the first test fails if
`LANGSMITH_API_KEY` or `LANGCHAIN_API_KEY` is set. The FI keys are
placeholders. The tests pin `LANGCHAIN_REVISION_ID` so the exported
`revision_id` does not depend on the checkout.

From the repository root, Python 3.11:

```bash
env -u PYTHONPATH PYTHONPATH="python/tests" \
  uv run --no-project --python 3.11 \
  --with 'pytest==9.1.1' --with 'langsmith[otel]==0.14.4' \
  --with 'opentelemetry-sdk==1.45.0' --with 'opentelemetry-exporter-otlp-proto-http==1.45.0' \
  pytest python/examples/langsmith/tests --confcutdir python/examples/langsmith \
  -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

`--confcutdir` keeps pytest from importing `python/__init__.py`, which needs
traceAI's own dependencies. For Python 3.10, 3.12 or 3.13, replace
`--python 3.11`. Without
`--with 'langsmith[otel]==0.14.4'` (and the two OpenTelemetry pins) the
SDK-half tests are skipped and the fixture tests still run.

Two tests are opt-in. They read fi-collector's span-type rules and alias
lists from its source (`exporter/clickhouse25exporter/converter.go` and
`pkg/adapter/adapter.go`) and check the "Future AGI type" columns above, the
fixture's types, and its model, provider and token values against them.
Point `FI_COLLECTOR_SRC` at a fi-collector checkout (the directory holding
`exporter/` and `pkg/`), then run the command above:

```bash
export FI_COLLECTOR_SRC=<future-agi checkout>/fi-collector
```

Without it they are skipped. No CI job runs this example, so run them
whenever fi-collector's rules or this recipe change; otherwise the tables
above can drift unnoticed.
