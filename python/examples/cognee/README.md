# Cognee → Future AGI (recipe)

There is no `traceai-cognee` package. Cognee creates its own OpenTelemetry
spans when `COGNEE_TRACING_ENABLED=true`; this recipe sends those spans to
Future AGI. It adds no instrumentor (a second one would double every span)
and never calls Cognee's `setup_tracing` (Cognee calls it on its first traced
operation). Option A adds one small export filter,
[`src/cognee_filter.py`](src/cognee_filter.py), that keeps Cognee's content
out of the export and marks search and LLM spans.

Pinned: `cognee==1.6.2` (Python >=3.10,<3.15; option A also needs
`fi-instrumentation-otel`, which declares Python <3.14). Tested on Python 3.11
and 3.13 against loopback fakes only: no LLM key, no live Future AGI project
(see [Tests](#tests)). Traces only: metrics and logs are not ingested by this
recipe. Statements marked "source reading" come from the cognee 1.6.2 code and
are not exercised by the tests.

## Option A: `register()` (recommended)

```bash
pip install -r requirements.txt

export FI_API_KEY=...            # sent as the X-Api-Key header
export FI_SECRET_KEY=...         # sent as the X-Secret-Key header
export FI_PROJECT_NAME=my-cognee-app
export COGNEE_TRACING_ENABLED=true
# Cognee's own model settings, unchanged by this recipe:
export LLM_API_KEY=...           # plus LLM_MODEL, EMBEDDING_* as usual

python src/app.py "Who works on Lighthouse?"
```

`register()` posts OTLP/HTTP protobuf to `https://api.futureagi.com/tracer/v1/traces`
(override the origin with `FI_BASE_URL`) with the `X-Api-Key` and
`X-Secret-Key` headers, and stamps every batch with the resource attributes
`project_name` and `project_type=observe`.

The whole integration is [`src/app.py`](src/app.py) plus
[`src/cognee_filter.py`](src/cognee_filter.py), which you copy next to it:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import BatchSpanProcessor, HTTPSpanExporter

from cognee_filter import CogneeExportFilter

provider = register(
    project_name=os.environ["FI_PROJECT_NAME"],
    project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=True,  # Cognee reuses the global provider
    verbose=False,
)
exporter = CogneeExportFilter(HTTPSpanExporter())
provider.add_span_processor(BatchSpanProcessor(exporter))  # required, see below

with provider.get_tracer(__name__).start_as_current_span("remember_and_ask"):
    asyncio.run(remember_and_ask(question, document))  # cognee.add / cognify / search
```

These things about it are load-bearing:

- **Register before the first Cognee call.** When Cognee sets up tracing it
  checks for a global provider. If one exists it attaches to it; if not it
  creates and installs its own, and OpenTelemetry then ignores a later
  `register(set_global_tracer_provider=True)`.
- **Keep the `add_span_processor(BatchSpanProcessor(exporter))` line.**
  `register()` marks its exporter as a default that the next
  `add_span_processor()` call replaces (`fi_instrumentation/otel.py`,
  `TracerProvider.add_span_processor`). Cognee calls
  `add_span_processor()` on the global provider to attach its in-memory span
  buffer, so without this line the Future AGI exporter is dropped and
  **nothing is exported**: Cognee still buffers its spans, but no request
  leaves the process (the `no_readd` test shows this). Adding the processor
  yourself makes it a regular one that Cognee's call leaves alone, and it is
  where the export filter goes. `HTTPSpanExporter()` reads `FI_BASE_URL`,
  `FI_API_KEY` and `FI_SECRET_KEY` just as `register()` does.
- **Wrap the calls in one span if you want one trace.** Each Cognee API call
  starts its own root span, and `cognee.add` ends its `memory.store` span
  before ingestion runs (`cognee/api/v1/add/add.py`), so without a parent span
  one add/cognify/search sequence arrives as five traces. With the parent span
  it is one trace.

Do not also set `OTEL_EXPORTER_OTLP_ENDPOINT` with this option. Cognee ignores
it for traces when a provider is already registered, but its metrics and log
bridge read it and would send to it (source reading of
`cognee/modules/observability/metrics.py` and `logs.py`; not exercised by the
tests).

### The export filter

`CogneeExportFilter` wraps the exporter and changes only what that exporter
sends. It exports copies, so Cognee's own span buffer and any other processor
or exporter still see the original spans.

- **Content is off by default.** It removes the five attributes in which
  Cognee 1.6.2 exports content (see [Content](#content)):
  `langfuse.observation.input`, `langfuse.observation.output`,
  `memory.query.text`, `cognee.search.query` and `cognee.db.query`. Where the
  same text also appears in that span's status description or event
  attributes (exception messages and stack traces), it is replaced with
  `__REDACTED__`. `CogneeExportFilter(exporter, capture_content=True)`, or
  `COGNEE_FI_CAPTURE_CONTENT=true` in the environment, exports everything.
- **Search and LLM spans get a type.** Cognee sets no span-kind attribute,
  so the filter sets `fi.span.kind` by span name, only where none of
  `fi.span.kind`, `gen_ai.span.kind`, `llm.request.type` and
  `openinference.span.kind` is set: `RETRIEVER` on `memory.retrieve` and
  every span whose name starts with `cognee.search` (in 1.6.2,
  `cognee.search.authorize` and `cognee.search.dataset`), `LLM` on
  `cognee.observe.acreate_structured_output` (every LLM call in Cognee 1.6.2). Embedding spans are left as they are:
  fi-collector already types them from `gen_ai.operation.name=embeddings`.
- **It fails closed.** If the filter raises on a span, that span is exported
  without the content attributes, events or status description; if even that
  fails, the span is dropped. The filter logs a warning and never raises into
  the export path.

What it does not catch: text that appears in an exception message or stack
trace but is not the value (or, for the JSON prompt, a field) of a removed
attribute on the same span. Examples: the part of a long question beyond
Cognee's 500-character cap, or a model response quoted in a parsing error
before Cognee recorded it. The tests plant the document, the question and the
answer and find none of them on the wire.

## Option B: Cognee's own OTLP exporter (no traceAI)

Cognee can export by itself when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and no
provider was registered:

```bash
export COGNEE_TRACING_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.futureagi.com:443/tracer/v1/traces
export OTEL_EXPORTER_OTLP_HEADERS="X-Api-Key=$FI_API_KEY,X-Secret-Key=$FI_SECRET_KEY"
export OTEL_RESOURCE_ATTRIBUTES="project_name=my-cognee-app,project_type=observe"
```

**Option B exports content and has no kind mapping.** The export filter
cannot be attached to Cognee's own exporter, so prompts, document text,
answers and search questions are exported (see [Content](#content)), and
search and LLM spans are stored as `unknown`. Option A is the privacy-safe
path.

- **Full path.** Cognee passes the endpoint string to the OTLP exporter
  unchanged; it does not append `/v1/traces`. The test configures
  `<origin>/tracer/v1/traces` and the request arrives at exactly
  `/tracer/v1/traces`.
- **Keep `:443` in the URL.** Cognee picks HTTP only for URLs containing
  `:4318`, `:443/` or a few vendor paths; for any other URL it uses the gRPC
  exporter whenever `opentelemetry-exporter-otlp-proto-grpc` is installed
  (`cognee[tracing]` installs it). Cognee's own comment warns that a gRPC
  exporter pointed at an HTTP-only endpoint fails silently.
  `https://api.futureagi.com/tracer/v1/traces` would go to gRPC;
  `https://api.futureagi.com:443/tracer/v1/traces` stays on HTTP (the test
  checks Cognee's own classifier for both).
- **`project_name` is required.** Cognee builds its resource with
  OpenTelemetry's `Resource.create`, which reads `OTEL_RESOURCE_ATTRIBUTES`;
  the test shows `project_name` and `project_type=observe` arrive that way.
  fi-collector rejects a batch in which any resource lacks `project_name`.
- **Headers are split naively** (source reading). Cognee parses
  `OTEL_EXPORTER_OTLP_HEADERS` itself: split on `,`, then on the first `=`,
  with no URL decoding. A key that contains a comma cannot be passed this way;
  use option A.
- **Expect 404s for logs.** Cognee also sends its log records to the same URL
  with `/v1/traces` replaced by `/v1/logs`, and (source reading) its metrics
  to `/v1/metrics`. fi-collector serves traces only; the test run logs
  `Failed to export logs batch code: 404`. Spans are unaffected.

## Spans Cognee 1.6.2 emits

From the recipe run (one `add`, one `cognify`, one `GRAPH_COMPLETION`
search). "Future AGI type" is what fi-collector derives: it reads the span
kind from `fi.span.kind`, `gen_ai.span.kind`, `llm.request.type` or
`openinference.span.kind`, falls back to `gen_ai.operation.name`, and
otherwise stores `unknown`. Cognee sets none of the first four; with option A
the export filter sets `fi.span.kind` on the spans marked below.

| Span | OTel kind | Future AGI type (option A / option B) | Notable attributes |
|---|---|---|---|
| `remember_and_ask` | INTERNAL | unknown | the app's own parent span (option A example) |
| `memory.store` | INTERNAL | unknown | `memory.operation=store`, `cognee.dataset.name` |
| `memory.process` | INTERNAL | unknown | `memory.operation=process`, `cognee.pipeline.name=cognify` |
| `memory.retrieve` | INTERNAL | retriever / unknown | `memory.query.text`, `cognee.search.query`, `cognee.search.type`, `memory.result.count` |
| `cognee.pipeline.task.resolve_data_directories`, `cognee.pipeline.task.ingest_data`, `cognee.pipeline.task.classify_documents`, `cognee.pipeline.task.extract_chunks_from_documents`, `cognee.pipeline.task.extract_graph_and_summarize`, `cognee.pipeline.task.add_data_points` | INTERNAL | unknown | `cognee.pipeline.task_name`, `cognee.result.count`, `cognee.result.summary` |
| `cognee.observe.acreate_structured_output` (every LLM call) | CLIENT | llm / unknown | `gen_ai.request.model`, `gen_ai.system=litellm-native`, `langfuse.observation.input`, `langfuse.observation.output`; `cognee.llm.model` on schema-bound calls only |
| `cognee.observe.embed_text` (every embedding call) | CLIENT | embedding | `gen_ai.operation.name=embeddings`, `gen_ai.request.model`, `gen_ai.provider.name`, `gen_ai.embeddings.dimension.count` |
| `cognee.llm.completion` | INTERNAL | unknown | prompt file name and lengths only |
| `cognee.db.graph.query`, `cognee.db.vector.search` | INTERNAL | unknown | `cognee.db.system`, `cognee.db.query`, `cognee.vector.collection` |
| `cognee.search.authorize`, `cognee.search.dataset` | INTERNAL | retriever / unknown | `cognee.search.query`, `cognee.search.type`, dataset name and id |
| `cognee.retrieval.get_objects`, `cognee.retrieval.triplet_search`, `cognee.retrieval.vector_search`, `cognee.retrieval.embed_query`, `cognee.retrieval.get_context`, `cognee.retrieval.get_completion` | INTERNAL | unknown | retriever name, counts and lengths |
| `cognee.session.get_session`, `cognee.session.add_qa` | INTERNAL | unknown | `cognee.session.id` |

With option A, the content attributes in this table are removed unless you
turn content capture on.

What that means in Future AGI:

- **Model column: populated.** Every LLM span carries `gen_ai.request.model`,
  which fi-collector promotes. The provider comes from `gen_ai.system`
  (`litellm-native`, Cognee's adapter name, not the upstream provider). No
  processor that copies `cognee.llm.model` is needed, so none is written.
- **Token and cost columns: empty.** Cognee 1.6.2 records no token usage on
  any span.
- **Search is shown as a retriever, LLM calls as LLM (option A).** The
  export filter sets `fi.span.kind`. With option B they stay `unknown`: no
  span-kind key arrives and only embedding spans carry
  `gen_ai.operation.name`.
- Some `cognee.db.vector.search` spans end in `ERROR` with an `exception`
  event (Cognee probes collections that do not exist yet). That is Cognee's
  own behaviour, not an export failure.

## Content

With tracing on, Cognee 1.6.2 writes content to span attributes by default,
and it has no setting that keeps tracing on but leaves content out. Option A's
export filter removes these attributes before export unless content capture
is on; option B exports them:

| Attribute | On | Contains |
|---|---|---|
| `langfuse.observation.input` | every LLM span | the LLM call's string arguments as JSON: Cognee's system prompt plus the document text (extraction, summarization), or the user question and retrieved graph context (search). First 8000 characters, after Cognee's regex secret redaction. |
| `langfuse.observation.output` | every LLM span | the model's response (extracted graph JSON, summaries, the answer). Same cap and redaction. |
| `memory.query.text`, `cognee.search.query` | `memory.retrieve`, `cognee.search.authorize` | the search question (first 500 characters). |
| `cognee.db.query` | `cognee.db.graph.query` | the graph query text (first 500 characters, redacted). In the test run these are parameterised queries without document text. |
| `exception.stacktrace` | error spans' `exception` events | Python stack traces, including absolute file paths of the host's Python install. Not removed by the filter (only content text in them is replaced). |

The test plants markers in the document and the question and asserts that,
with option A's defaults, none of them arrives anywhere (attributes, status,
events), and that with content capture on they arrive under exactly these
keys and nowhere else. Embedding spans carry metadata only, not the embedded
text.

Not exported: the LLM and embedding API keys and the Future AGI keys (those
travel only as request headers). The test asserts both.

With option B, the only way to keep content out of Future AGI is to leave
`COGNEE_TRACING_ENABLED` unset: the `tracing_off` test shows Cognee then emits
no spans (only the app's own parent span remains). If `LANGFUSE_PUBLIC_KEY`
and `LANGFUSE_SECRET_KEY` are set, Cognee turns tracing on by itself; set
`COGNEE_TRACING_ENABLED=false` to veto that (source reading).

`TELEMETRY_DISABLED=1` is a different switch: it turns off Cognee's anonymous
product telemetry, not OpenTelemetry tracing.

## Troubleshooting

- **No traces at all with option A.** The `add_span_processor(BatchSpanProcessor(exporter))`
  line is missing, or `register()` ran after Cognee's first traced call.
- **Prompts or documents visible with option A.** `COGNEE_FI_CAPTURE_CONTENT=true`
  is set, `capture_content=True` is passed, or the exporter is not wrapped in
  `CogneeExportFilter`.
- **Spans doubled.** Another instrumentor is wrapping Cognee or LiteLLM on top
  of Cognee's own spans. Remove it.
- **Option B exports nothing.** The URL has no `:443`, so Cognee chose gRPC.
- **404 on `/tracer/v1/traces/v1/traces`.** Something appended the path to a
  URL that already had it. Cognee does not append; give the full path once.
- **Five traces per request.** Wrap the Cognee calls in one parent span.
- **No token counts.** Cognee 1.6.2 does not record them.

## Tests

`tests/test_cognee_recipe.py` runs, side by side, five subprocesses with the
real `cognee==1.6.2`. Each loads `tests/loopback_guard/sitecustomize.py`
through `PYTHONPATH`, and so do the worker processes Cognee spawns for
LanceDB and Ladybug: in all of them, Python-level connections and DNS lookups
to any host other than 127.0.0.1 are refused and logged, and the test asserts
the log is empty and that the guard was installed in Cognee's workers. Native
code that opens sockets itself (for example a database extension download) is
not intercepted. Cognee's LLM and embedding endpoints are a loopback fake of
the OpenAI API (`tests/_fake_openai.py`), and spans go to the shared harness
`Receiver` (`python/tests/harness`), which serves `/v1/traces` and
`/tracer/v1/traces` like fi-collector but does not authenticate or store
anything:

- `recipe`: `src/app.py` as written (option A, content off).
- `capture`: the same, with `COGNEE_FI_CAPTURE_CONTENT=true`.
- `tracing_off`: the same, with `COGNEE_TRACING_ENABLED` unset.
- `no_readd`: `register()` without the re-add line, then one `cognee.add`.
- `otlp_env`: option B's environment variables, then one `cognee.add`.

`tests/fixtures/cognee-1.6.2-recipe-spans.json` is Cognee's own spans from
the recipe run as the Receiver decoded them, before the export filter
(host paths replaced by `<python-lib>`, `<repo>`, `<tmp>` and `<home>`). The
fixture tests post it with the harness `post_otlp()`, check it with
`compare()`, run the same mapping and content assertions on it, and send it
through the export filter, with no Cognee installed. The live runs must match
the filtered fixture's shape (span names, kinds, attribute keys, statuses and
counts). Re-record with `COGNEE_RECORD_FIXTURE=1` (it records the `capture`
run without the `fi.span.kind` the filter adds).

From the repository root:

```bash
env -u PYTHONPATH PYTHONPATH="python/examples/cognee:python:python/tests" \
  uv run --no-project --python 3.11 \
  --with pytest --with pytest-asyncio --with opentelemetry-api \
  --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with wrapt --with requests \
  --with jsonschema --with protobuf --with opentelemetry-proto \
  --with 'cognee==1.6.2' \
  pytest python/examples/cognee/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Importing Cognee takes about a minute, so a full run takes a few minutes.
`register()` comes from this repository's `python/fi_instrumentation`, not a
published release. The environment above has no gRPC exporter installed;
option B's gRPC choice is covered by calling Cognee's URL classifier, not by a
gRPC export.
