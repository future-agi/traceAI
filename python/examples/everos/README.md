# EverOS → Future AGI (recipe)

EverOS here means EverMind AI's open-source memory framework, repository
`EverMind-AI/EverOS`, published on PyPI as `everos`. It is not EverMemOS
Cloud (or its `evermemos-mcp` cloud connector), and it is not the legacy
Claude Code plugin; this page sets up neither. There is no `traceai-everos`
package: EverOS has exported its own OpenTelemetry spans since 1.2.0, off by
default, and this recipe only turns that export on and points it at Future
AGI. It adds no instrumentor, no span processor and no HTTP wrapper.

Pinned: `everos[otel]==1.4.1`, the latest release on PyPI on 2026-10-05
(released 2026-09-24), `Requires-Python: >=3.12`. Its wheel METADATA declares
`License: Apache-2.0` (license files `LICENSE` and `NOTICE`). It is a
dependency of this example only. The `otel` extra installs
`opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` (`>=1.27.0`);
`requirements.txt` pins 1.45.0 of both, the versions tested. Only 1.4.1 was
tested; 1.2.0, the release that added tracing, was not. Tested on Python 3.12
and 3.13 against loopback fakes only (see [Tests](#tests)). Statements marked
"source reading" come from the everos 1.4.1 wheel or from Future AGI source
and are not exercised by the tests.

## Set up

```bash
cd python/examples/everos
pip install -r requirements.txt
everos init                      # creates ~/.everos/everos.toml; set your [llm] and [embedding] keys there as usual

export FI_API_KEY="YOUR_FI_API_KEY"
export FI_SECRET_KEY="YOUR_FI_SECRET_KEY"
export EVEROS_OBSERVABILITY__ENABLED=true
export EVEROS_OBSERVABILITY__ENDPOINT="https://api.futureagi.com/tracer/v1/traces"
export EVEROS_OBSERVABILITY__HEADERS="$(python -c 'import json, os; print(json.dumps({"X-Api-Key": os.environ["FI_API_KEY"], "X-Secret-Key": os.environ["FI_SECRET_KEY"]}))')"
export OTEL_RESOURCE_ATTRIBUTES="project_name=my-everos-app,project_type=observe"

everos server start
```

Content capture stays at EverOS's default, off. The same settings can live in
the `[observability]` section of `~/.everos/everos.toml` instead (environment
variables win over the file). `everos init` already wrote an
`[observability]` section, so edit that one rather than adding a second:

```toml
[observability]
enabled = true
endpoint = "https://api.futureagi.com/tracer/v1/traces"
capture_content = false

[observability.headers]
X-Api-Key = "YOUR_FI_API_KEY"
X-Secret-Key = "YOUR_FI_SECRET_KEY"
```

The file then holds your Future AGI keys in plain text, as it already does
for your model keys. `OTEL_RESOURCE_ATTRIBUTES` is needed either way.

EverOS reads `[observability]` (`config/settings.py:624-668`) from, in order
of priority, `EVEROS_OBSERVABILITY__<KEY>` variables, `<root>/everos.toml`
and its shipped `config/default.toml:219-238` (`settings.py:692-697,
725-746`). The server's startup builds the tracer from it
(`core/lifespan/tracing_lifespan.py:36-45`, registered first in
`entrypoints/api/app.py:85-95`). Paths in this page are relative to the
`everos` package in the 1.4.1 wheel.

### Endpoint

EverOS passes `endpoint` to OpenTelemetry's OTLP/HTTP exporter as given
(`core/observability/tracing/provider.py:134-138`); only when it is empty
does the exporter fall back to OpenTelemetry's own variables. Tested results:

| Setting | Request path |
|---|---|
| `EVEROS_OBSERVABILITY__ENDPOINT=<origin>/tracer/v1/traces` | `/tracer/v1/traces` |
| `EVEROS_OBSERVABILITY__ENDPOINT=<origin>` (no path) | `/`, which fi-collector answers with 404: nothing is stored |
| no EverOS endpoint, `OTEL_EXPORTER_OTLP_ENDPOINT=<origin>` | `/v1/traces` (appended by the exporter) |

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is also used as given, and with nothing
set the exporter posts to `http://localhost:4318/v1/traces` (source reading,
`opentelemetry/exporter/otlp/proto/http/_common/__init__.py:129-138`).
fi-collector serves `/v1/traces` and `/tracer/v1/traces`
(`fi-collector/pkg/server/server.go:233-234`); traceAI's own exporter uses
`https://api.futureagi.com` plus `/tracer/v1/traces`
(`python/fi_instrumentation/settings.py:15`, `otel.py:719`). These paths were
tested against a loopback receiver; no deployed endpoint was called.

### Auth headers

EverOS's `headers` setting is a table of header names to values:
`[observability.headers]` in the file, or `EVEROS_OBSERVABILITY__HEADERS` as a
JSON object (pydantic-settings parses complex values from the environment as
JSON). The values are sent as given; the test's secret contains a comma and
an `=` and arrives intact. fi-collector reads `X-Api-Key` and `X-Secret-Key`
(`fi-collector/pkg/auth/middleware.go:41-49`).

OpenTelemetry's own `OTEL_EXPORTER_OTLP_TRACES_HEADERS` (or
`OTEL_EXPORTER_OTLP_HEADERS`) also works when EverOS sets no headers (tested).
It is comma-separated `name=value` pairs with each value percent-decoded, so
percent-encode a key that contains a comma. With exporter 1.45.0 the two
sources are merged and EverOS's value wins for a header set in both (tested;
`_common/__init__.py:141-153`).

Leave the `langfuse_public_key`, `langfuse_secret_key` and `langfuse_host`
settings unset. When all three are set, EverOS adds an
`Authorization: Basic` header for Langfuse, fills in a Langfuse endpoint if
none is set (`provider.py:73-93`), and pushes recall scores to the Langfuse
REST API outside the span stream (`tracing/scores.py:136-173`). Source
reading.

### Project

fi-collector rejects a batch whose resource has no `project_name`
(`fi-collector/pkg/auth/stamp.go:31-45`). EverOS has no setting for resource
attributes: it builds the resource with OpenTelemetry's `Resource.create`
and adds only `service.name` (`[observability] service_name`, default
`everos`) and `service.version` (`provider.py:127-129`). `Resource.create`
reads `OTEL_RESOURCE_ATTRIBUTES`, so that variable carries `project_name`
and `project_type=observe` (tested; without it the resource has no
`project_name`, also tested).

### Where tracing starts

`everos server start` builds the tracer at startup and flushes it at
shutdown. EverOS keeps its `TracerProvider` to itself rather than installing
it as OpenTelemetry's global one, and exports through its own
`BatchSpanProcessor` (`provider.py:44-45, 130-139`). If you use EverOS as a
library without its server, call
`init_tracing(load_settings().observability)` from
`everos.core.observability.tracing` and `everos.config` before the first
memory call and `shutdown_tracing()` at exit. `tests/everos_config_probe.py`
calls the two around one EverOS span; driving EverOS's memory calls that way,
without the server, was not tested.

## Spans EverOS 1.4.1 emits

From the recorded scenario (one `add`, one `flush`, one hybrid `search`),
with capture off. Every span has OTel kind `INTERNAL`, the tags
`langfuse.trace.tags = ["everos", "memory"]` and EverOS's own type in
`langfuse.observation.type` (`core/observability/tracing/attributes.py:96-136`).
"Architecture kind" is the kind table of the TH-8329 architecture, which is
not implemented anywhere yet (see the next section).

| Span | Parent | `langfuse.observation.type` | Architecture kind | Other attributes |
|---|---|---|---|---|
| `everos.memory.add` | none (one trace per call) | `span` | CHAIN | `langfuse.session.id`, `langfuse.trace.metadata.{mode,is_final,defer_extraction,request_id}` |
| `everos.memory.flush` | none | `span` | not listed | same as `everos.memory.add` |
| `everos.memcell.boundary` | `everos.memory.add`, `everos.memory.flush` | `generation` | LLM | `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| `everos.extract` | `everos.memory.flush` | `generation` | LLM | the `gen_ai.*` keys above, `langfuse.session.id`, `langfuse.trace.metadata.{app_id,project_id,memcell_id}` |
| `everos.persist.markdown` | `everos.memory.flush` | `span` | not listed | `langfuse.session.id`, `langfuse.trace.metadata.{owner_id,app_id,project_id}` |
| `everos.ome.extract_agent_case` | `everos.memory.flush` | `agent` | not listed | `langfuse.trace.metadata.{strategy,run_id,attempt,event_topic}` |
| `everos.ome.extract_atomic_facts` | `everos.memory.flush` | `agent` | not listed | the same, plus the `gen_ai.*` keys of its LLM call |
| `everos.ome.trigger_profile_clustering` | `everos.memory.flush` | `agent` | not listed | `langfuse.trace.metadata.{strategy,run_id,attempt,event_topic}` |
| `everos.ome.extract_user_profile` | `everos.ome.trigger_profile_clustering` | `agent` | not listed | the same |
| `everos.embedding` | `everos.search.recall`, `everos.ome.trigger_profile_clustering` | `embedding` | EMBEDDING | `gen_ai.request.model`, `gen_ai.usage.input_tokens` |
| `everos.memory.search` | none | `retriever` | RETRIEVER | `langfuse.user.id`, `langfuse.trace.metadata.{method,owner_type,app_id,project_id,request_id}`, `everos.search.top_score`, `everos.search.hit` |
| `everos.search.recall` | `everos.memory.search` | `retriever` | RETRIEVER (`everos.search.*`) | `langfuse.trace.metadata.{phase,method}` |
| `everos.search.rank` | `everos.memory.search` | `span` | RETRIEVER (`everos.search.*`) | `langfuse.trace.metadata.phase` |

1.4.1 renamed none of the architecture's span names. The `everos.ome.*`
spans come from EverOS's background strategy scheduler; they join the
`flush` trace (`infra/ome/_dispatch/runner.py:224-238`), and which ones run
depends on the conversation. Other spans in the 1.4.1 source that this
scenario does not produce (source reading): `everos.memory.buffer` (an `add`
with `defer_extraction`, `service/memorize.py:211-217`),
`everos.reflect.consolidate` (`memory/reflection/orchestrator.py:580-583`),
other `everos.ome.<strategy>` names, and `everos.search.recall` /
`everos.search.rank` with other phases for keyword, vector, agentic and
agent-memory searches (`memory/search/manager.py`, `callbacks.py`,
`agentic.py`). Embedding calls made while EverOS indexes in the background
open no span (`nested_only`, `component/embedding/openai_provider.py:117-123`).

## What Future AGI derives today

- **Kind: `unknown` for every EverOS span.** fi-collector reads a span kind
  from `fi.span.kind`, `gen_ai.span.kind`, `llm.request.type` or
  `openinference.span.kind`, then falls back to `gen_ai.operation.name`
  (`fi-collector/exporter/clickhouse25exporter/converter.go:79-131`). EverOS
  sets none of them (tested), so fi-collector stores `unknown` (source
  reading of the collector). The architecture's kind table is therefore an
  open SF-1 gap, not something this recipe delivers.
- **No processor here.** The PRD (r2, section 0) records SF-1 as decided:
  span-kind mapping lives in shared traceAI processors and no child copies
  it, so the architecture's condition for a per-child processor ("only if
  SF-1 is not the home") does not hold. It also could not attach: EverOS
  creates its own provider and processor inside `init_tracing` and the
  server passes no processor of yours (`tracing_lifespan.py:42`).
- **EverOS already types its spans** in `langfuse.observation.type`. The
  backend's Langfuse adapter (future-agi `main` 4af5338,
  `futureagi/tracer/utils/adapters/langfuse.py:40-51, 83-88, 124-131`) maps
  that key: `generation` to LLM, `span` to CHAIN, `retriever` to RETRIEVER,
  `embedding` to EMBEDDING, `agent` to AGENT. fi-collector's converter does
  not read it. Under that map every architecture row would come out as
  specified except `everos.search.rank`, which EverOS types `span` (CHAIN),
  not RETRIEVER. Source reading; whether that adapter runs on
  fi-collector-ingested spans was not established. Recorded here for SF-1.
- **Model: populated.** `gen_ai.request.model` is on generation and
  embedding spans; fi-collector reads it (`fi-collector/pkg/adapter/adapter.go:279-284`).
  EverOS sets no provider key, so the provider column stays empty
  (`adapter.go:289-295`).
- **Tokens: on generation and embedding spans only.**
  `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`
  (`adapter.go:296-305`); embedding spans carry input tokens only. EverOS
  writes no total; fi-collector adds input and output (`adapter.go:232-234`).
  A span that wraps several LLM calls carries their sum
  (`attributes.py:191-221`).
- **Cost: no cost attribute is exported** (tested), and the recipe converts
  no tokens to a price. fi-collector's own fallback is a different matter:
  with no `gen_ai.cost.*` / `llm.cost.*` attribute (`adapter.go:311-313`) it
  prices the tokens itself when its price table knows the model
  (`converter.go:285-294`). Whether a stored EverOS span shows a cost
  therefore depends on that table and the model name you configure
  (EverOS's default is the OpenRouter-style `openai/gpt-4.1-mini`). Source
  reading; not tested.

## Privacy

Content capture is off unless `capture_content = true`
(`EVEROS_OBSERVABILITY__CAPTURE_CONTENT=true`); the default is false
(`settings.py:658`, `default.toml:228-230`, `attributes.py:43-45`).

- **Capture off (tested):** no question, no message text and no extracted
  memory anywhere in the export, and no `langfuse.observation.input` or
  `langfuse.observation.output` key.
- **Capture on (tested):** exactly four attributes are added.
  `everos.memory.search` gets `langfuse.observation.input` (the query,
  `top_k` and method as JSON) and `langfuse.observation.output` (the ids of
  the returned memories); `everos.extract` gets `langfuse.observation.output`
  (the extracted episode text); `everos.persist.markdown` gets
  `langfuse.observation.output` (the written file's path relative to the
  memory root). Source reading: values are cut to 4096 characters
  (`attributes.py:39, 61-65`), and although EverOS calls them "redacted",
  the redaction hook is a no-op unless your code installs one with
  `set_redactor` (`attributes.py:46, 55-58`).
- **Never exported (tested, both settings):** the text of the messages you
  add, the Future AGI keys (they travel only as request headers) and the
  model API key.
- **Exported even with capture off (tested):** identifiers. Session id
  (`langfuse.session.id`), the searched user id (`langfuse.user.id`), the
  memory owner (`langfuse.trace.metadata.owner_id`), app and project ids,
  memcell, request and run ids, model names, and the search's top score.
- **Not covered by the switch (tested):** when an extraction fails,
  everalgo's error message quotes the model's reply, and OpenTelemetry
  records that message as the span's status and in an `exception` event
  with the stack trace. The `llm_error` test finds the reply there with
  capture off.

## Notes

- Spans leave in batches and on server shutdown (`provider.py:135-138,
  169-183`). `sample_rate` (default 1.0) samples whole traces.
- `everos config show` does not print the `[observability]` section
  (`entrypoints/cli/commands/config_cmd.py:34-48`), so it does not echo the
  header values. Source reading.
- Not tracing-specific, but found by the tests' network guard: on the first
  `add`, everalgo loads tiktoken's `o200k_base` encoding
  (`everalgo/_tokenize.py:14-19`), and tiktoken downloads it from
  `openaipublic.blob.core.windows.net` unless it is cached. An offline host
  needs that file in tiktoken's cache. The tests register an offline
  stand-in instead.
- No call to Future AGI, a model provider or Langfuse was made. Auth
  rejection (401 without keys), project stamping and storage are
  fi-collector behaviour the harness receiver does not reproduce.

## Tests

`tests/test_everos_recipe.py` has two parts.

Fixture tests need no EverOS. `tests/fixtures/everos-1.4.1-capture-off.json`
and `everos-1.4.1-capture-on.json` are the spans EverOS 1.4.1 exported in the
live scenario below, with content capture off and on, as the harness
`Receiver` decoded them; `everos-1.4.1-resource.json` is the capture-off
run's resource. The tests post each with the harness `post_otlp()`, check
the result with `compare()`, and assert this page's facts on it: span names
and tree, `langfuse.observation.type` per span (the table above), no
fi-collector kind key, no cost key, tokens and models only where EverOS sets
them, no query or other content with capture off, the query on the search
span with capture on, `project_name` and `project_type=observe` on the
resource, and no keys or host paths.

Live tests run when `everos` is installed. `tests/everos_session.py` runs
`everos init`, then drives EverOS's own app (`create_app()`, what
`everos server start` serves) in-process with Starlette's `TestClient`: no
port is bound and no server is started. It adds three messages, flushes,
waits for indexing and runs one hybrid search. EverOS's LLM and embedding
endpoints are a loopback fake of the OpenAI API (`tests/_fake_openai.py`),
and spans go to the shared harness `Receiver` (`python/tests/harness`),
which serves `/v1/traces` and `/tracer/v1/traces` like fi-collector but does
not authenticate or store anything. Three runs: the recipe (capture off),
capture on, and a model reply everalgo rejects (`llm_error`). They must
match the fixtures (the background `everos.ome.*` spans only where they
finished), and the tests check the request path, both auth headers, the
resource, content off and on, and that no key reaches the export or the
output. `tests/everos_config_probe.py` exports one span through EverOS's
settings and tracer to test the toml block above, tracing off by default,
the capture switch, the OpenTelemetry header and endpoint variables, an
endpoint without its path, and a missing `OTEL_RESOURCE_ATTRIBUTES`.

Every live process loads `tests/loopback_guard/sitecustomize.py` through
`PYTHONPATH`: Python-level connections and DNS lookups to any host other
than 127.0.0.1 are refused and logged, and the tests assert the log is empty.
A positive control (`tests/_guard_probe.py`) proves the guard refuses and
logs IPv4, IPv6 and DNS attempts. Native code that opens sockets itself is
not intercepted. All keys are placeholders.

From the repository root, Python 3.12:

```bash
env -u PYTHONPATH PYTHONPATH="python:python/tests" \
  uv run --no-project --python 3.12 \
  --with pytest --with 'everos[otel]==1.4.1' \
  --with 'opentelemetry-sdk==1.45.0' --with 'opentelemetry-exporter-otlp-proto-http==1.45.0' \
  --with requests --with jsonschema --with wrapt \
  pytest python/examples/everos/tests -q -p no:cacheprovider \
  --noconftest -o addopts= -rfEs
```

For Python 3.13, replace `--python 3.12`. `python` is on `PYTHONPATH` and
`requests`, `jsonschema` and `wrapt` are installed only because pytest
imports the repository's `python/__init__.py` package (which imports
`fi_instrumentation`) when it collects these tests; the tests use nothing
from it. Re-record the fixtures by running the same command with
`EVEROS_RECORD_FIXTURE=1`. A full run takes under a minute once uv has the packages.

All 33 tests pass on Python 3.12.10 and 3.13.15 (macOS arm64) with the pins
above and what uv resolved on 2026-10-05: fastapi 0.142.2, starlette 1.7.0,
httpx 0.28.1, openai 2.54.0, pydantic 2.13.5, pydantic-settings 2.15.0,
lancedb 0.34.0, tiktoken 0.14.0, protobuf 7.36.2, opentelemetry-proto
1.45.0, and the everalgo packages everos pins (core 0.3.0, boundary 0.2.1,
user-memory 0.4.0, agent-memory 0.4.0, rank 0.4.1). Without `everos`
installed, the 17 fixture tests pass and the 16 live tests are skipped
(checked on 3.12). Python 3.10 and 3.11 were not run: EverOS requires 3.12
or newer.
