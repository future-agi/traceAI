# OpenInference → Future AGI (active-ingestion fixture)

There is no traceAI package for OpenInference and this directory adds none.
It is a conformance fixture: one hand-built OTLP/JSON request with the
OpenInference keys fi-collector already reads, posted with the shared harness
and checked against a golden file. The backend's Python adapter
(`futureagi/tracer/utils/adapters/openinference.py`) is a semantic reference,
not the system under test, and nothing here imports it.

Pin for the later round-trip step with a real instrumentor:
openinference-instrumentation 0.1.70 (`Requires-Python: <3.15,>=3.10`). It is
not installed here and the tests do not import it. No license is claimed for
it here. The fixture's keys are the ones its dependency,
`openinference-semantic-conventions` 0.1.41, defines.

Alpha, fixture-validated. The tests pass on Python 3.10, 3.11, 3.12 and 3.13
(see [Tests](#tests)). They post only to the
harness `Receiver` on 127.0.0.1: no fi-collector, no Arize account, no live
instrumentor. Sections marked "source reading" come from future-agi `main`
4af5338 and are not exercised by the always-on tests.

## The Django route is retired and is not proof

In future-agi `main` 4af5338, `futureagi/tracer/otel_compat_urls.py:15-16`
holds the old OTLP route only as a comment ("Migrated to fi-collector service
(June 2026)"), so its `urlpatterns` is empty. `tfc/urls.py:86` still mounts
the module under `api/public/otel/`, but it serves nothing. A response from
that path, or the existence of `openinference.py`, is not evidence that
OpenInference spans are ingested. This fixture does not uncomment the route,
post to it, or call the Python adapter. Ingestion is fi-collector's
`/v1/traces` and `/tracer/v1/traces` (`pkg/server/server.go:233-234`).

## What is posted

`tests/fixtures/openinference-trace.otlp.json`, three spans in one trace:

| Span | Parent | Attributes |
|---|---|---|
| `retrieve` | none | `openinference.span.kind=RETRIEVER`, `input.value` (the query) |
| `ChatCompletion` | `retrieve` | `openinference.span.kind=LLM`, `llm.model_name=gpt-4o-mini`, `llm.token_count.prompt=120`, `llm.token_count.completion=38` |
| `lookup_refund_policy` | `retrieve` | `openinference.span.kind=TOOL` |

The resource carries `service.name`, `project_name` and
`project_type=observe`. fi-collector fails a batch whose resource has no
`project_name` with HTTP 400 (`pkg/auth/stamp.go:31-45`, `server.go:461-464`).

Kind values are posted exactly as OpenInference sets them, in upper case. The
fixture sets no other key fi-collector reads a kind from (`fi.span.kind`,
`gen_ai.span.kind`, `llm.request.type`, `gen_ai.operation.name`), so the kind
can come only from `openinference.span.kind`.

The body is OTLP/JSON as fi-collector decodes it (pdata `UnmarshalJSON`,
`server.go:438-442`): hex trace and span ids, int64 values as decimal
strings, enums as integers. Base64 ids, which protobuf's `MessageToDict` (and
so the harness `Receiver`, for a protobuf export) produces, fail that decoder,
and the handler answers 400 (`server.go:440`).

`tests/fixtures/openinference-spans.golden.json` is the posted body's spans,
verbatim. It is the golden for `compare()`, not a rendered trace.

## What fi-collector does with it (source reading)

| Fixture key | fi-collector 4af5338 | Stored as |
|---|---|---|
| `openinference.span.kind` | fourth kind key (`exporter/clickhouse25exporter/converter.go:79-84`); lower-cased and checked against the type list (`:107-131`) | `observation_type` `retriever`, `llm`, `tool` |
| `llm.model_name` | first model alias (`pkg/adapter/adapter.go:279-284`) | `model` |
| `llm.token_count.prompt`, `llm.token_count.completion` | first input and output token alias (`adapter.go:296-305`) | `prompt_tokens`, `completion_tokens`; `total_tokens` is their sum when no total key is set (`adapter.go:230-235`) |
| `input.value` | overflow prefix (`adapter.go:32-40`), also lifted into the `input` column (`converter.go:343`) | `attributes_extra` and `input` |

So the collector's aliases cover kind, model and tokens for these keys, and
the retriever query stays readable without a new column. How the trace view
renders `observation_type` is shared processor work (SF-1), not this fixture.
OpenInference instrumentors also set `llm.provider` and `llm.system`; both are
provider aliases (`adapter.go:289-295`). The fixture sets neither.

Unknown kinds: anything not in the collector's type list
(`converter.go:64-68`) after lower-casing is stored as `unknown`
(`:127-129`). That function has no error path and `spanToRow` returns no error
(`:407`), so a kind value cannot make the export fail. Of the 12 kinds in
`openinference-semantic-conventions` 0.1.41, `PROMPT` and `DECISION` are not
in that list, and `UNKNOWN` and an empty kind are stored as `unknown` too. None
of the handler's 4xx answers (method, read, size, content type, decode, usage
limit, missing `project_name`, scope, convert) depends on the value of a span
attribute (`server.go:402-482`).

These rows were also checked once, offline and outside this repository's
tests, by running fi-collector 4af5338's own decoder and
`ConvertWithIdentities` on the fixture and on one-span variants with kinds
`PROMPT`, `DECISION`, `UNKNOWN`, `not-a-kind` and an empty kind. That run
started no server and had no auth, pricer or ClickHouse.

## What this fixture does not do

- Add a package, a private adapter, or a Go port of the Python adapter.
- Edit `openinference.py` or `otel_compat_urls.py`, reorder alias priority,
  or add a column for `input.value`.
- Reproduce fi-collector auth (401 without keys), project stamping, storage
  or the trace view. Those need a collector, which the harness does not start.

## Tests

`tests/test_openinference_fixture.py` posts the fixture with the harness
`post_otlp()` to the shared `Receiver` (`python/tests/harness`), which serves
`/v1/traces` and `/tracer/v1/traces` on 127.0.0.1 but does not authenticate,
resolve kinds or store anything, and checks the decoded spans with
`compare()` against the golden. So these tests assert what is posted, not
what is stored. They check:

- the golden match (names, attributes, statuses, and ids, parentage and
  times), with a control showing `compare()` fails when a kind value's case
  changes;
- one trace with `retrieve` as the parent of the other two spans;
- each kind string as posted, and no other kind key on any span;
- the model and token keys and their types, and the query in `input.value`;
- hex ids, and the resource's `project_name`;
- that kind values `PROMPT`, `DECISION`, an empty kind and `not-a-kind` post
  without error and arrive unchanged. The `Receiver` never reads a kind, so
  this checks only the body; the collector side is the source reading above;
- that `post_otlp()` refuses non-loopback endpoints before opening a
  connection (the control for the only network path), that no
  `openinference` or `tracer` module was imported, and that the fixture holds
  no key material. The harness sends no auth header and no key is used.

From the repository root:

```bash
env -u PYTHONPATH PYTHONPATH="python:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'pytest==9.1.1' --with 'opentelemetry-sdk==1.45.0' \
  --with 'opentelemetry-exporter-otlp-proto-http==1.45.0' \
  --with 'opentelemetry-instrumentation==0.66b0' \
  --with 'requests==2.34.2' --with 'jsonschema==4.26.0' \
  pytest python/examples/openinference/tests -q -p no:cacheprovider \
  --noconftest -o addopts= -rfEs
```

For Python 3.13, replace `--python 3.11`. The test itself needs only pytest.
pytest imports `python/__init__.py` (fi_instrumentation) for any test under
`python/`, which is why the other packages are listed. A run takes a few
seconds.

Two opt-in tests read fi-collector's Go source instead of copying its tables.
They check that the kind, model, token, `input.value` and `project_name`
facts above still hold, and that the fixture's kinds are in the collector's
type list and the unknown ones are not. Point `FI_COLLECTOR_SRC` at a
fi-collector checkout:

```bash
mkdir -p /tmp/fi-main
git -C <future-agi checkout> archive origin/main fi-collector | tar -x -C /tmp/fi-main
export FI_COLLECTOR_SRC=/tmp/fi-main/fi-collector   # then run the command above
```

Without it they are skipped. They read the source; they do not run the
collector. No CI job runs this directory, so run them whenever fi-collector's
kind or alias code changes.
