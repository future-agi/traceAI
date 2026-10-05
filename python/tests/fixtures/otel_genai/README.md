# OTel GenAI fixtures (era A, era B, dual-emit)

These are fixtures of these keys, not a claim that Future AGI implements a
named spec version. Each file is one OTLP/JSON export request carrying OTel
GenAI (`gen_ai.*`) attributes the way fi-collector receives them. The era
labels name key sets, not spec versions. A secondary review of the spec
history (not re-read from the spec repository here) dates two renames:
`gen_ai.usage.prompt_tokens`/`completion_tokens` became
`input_tokens`/`output_tokens` in v1.27, and `gen_ai.system` became
`gen_ai.provider.name` in v1.37. Era A uses the old names and era B the new
ones. Dual-emit sets two names for the same token count.

Pinned 2026-10-05:

- fi-collector at future-agi `main` `4af5338`
  (`fi-collector/pkg/adapter/adapter.go`).
- The retrieval operation name comes from
  `open-telemetry/semantic-conventions-genai` at
  `e07f4ebacb08f56db8c4c882d117720333fbca04`, `docs/gen-ai/gen-ai-spans.md`
  lines 597-599: "The gen_ai.operation.name SHOULD be `retrieval`"
  (status: Development). This change takes that quote from the TH-8339
  architecture note and did not fetch the page again.

The model name is the fake `gpt-test`. The fixtures carry no keys and no
message content.

## The fixtures

Every resource carries `project_name` and `project_type=observe`.
fi-collector fails a batch whose resource has no `project_name`
(`pkg/auth/stamp.go:31-46`, answered with 400 at `pkg/server/server.go:461-464`).

| File | Span | Keys | fi-collector columns |
|---|---|---|---|
| `era_a.json` | `chat` | `gen_ai.system=openai`, `gen_ai.usage.prompt_tokens=3`, `gen_ai.usage.completion_tokens=4`, `gen_ai.operation.name=chat` | provider `openai`; prompt, completion and total tokens 0 |
| `era_b.json` | `chat gpt-test` | `gen_ai.provider.name=openai`, `gen_ai.request.model=gpt-test`, `gen_ai.response.model=gpt-test-actual`, `gen_ai.usage.input_tokens=3`, `gen_ai.usage.output_tokens=4`, `gen_ai.operation.name=chat` | model `gpt-test`, provider `openai`, tokens 3 / 4 / 7 |
| `era_b.json` | `retrieval` | `gen_ai.operation.name=retrieval` only | none |
| `era_b.json` | `invoke_agent` | `gen_ai.operation.name=invoke_agent` only | none |
| `dual_emit.json` | `dual_emit` | `llm.token_count.prompt=3`, `gen_ai.usage.input_tokens=3`, `llm.token_count.completion=4`, `gen_ai.usage.output_tokens=4` | tokens 3 / 4 / 7, not 6 / 8 / 14 |
| `dual_emit.json` | `dual_emit prompt_tokens` | `gen_ai.usage.input_tokens=3` and `gen_ai.usage.prompt_tokens=3` | prompt tokens 3; total 3 |

In `era_b.json`, `invoke_agent` is the parent of `retrieval` and
`chat gpt-test`, all in one trace.

The columns come from fi-collector's alias lists (`adapter.go`, `4af5338`).
Each list is read in order and the first key present wins. Values are never
added together. If no total key is set, the total is prompt plus completion.

| Column | Alias list, in priority order | Lines |
|---|---|---|
| model | `llm.model_name`, `gen_ai.request.model`, `gen_ai.response.model`, `llm.request.model` | 279-284 |
| provider | `gen_ai.provider.name`, `llm.system`, `gen_ai.system`, `llm.vendor`, `llm.provider` | 289-295 |
| prompt tokens | `llm.token_count.prompt`, `gen_ai.usage.input_tokens`, `llm.usage.prompt_tokens` | 296-300 |
| completion tokens | `llm.token_count.completion`, `gen_ai.usage.output_tokens`, `llm.usage.completion_tokens` | 301-305 |
| total tokens | `llm.token_count.total`, `gen_ai.usage.total_tokens`, `llm.usage.total_tokens` (else prompt + completion) | 306-310, 230-235 |

What that means for each fixture:

- **Era B model is the request model.** `gen_ai.request.model` is ahead of
  `gen_ai.response.model`, so the stored model is `gpt-test`, not
  `gpt-test-actual`. This change does not reorder the list.
- **Era A tokens are a miss.** `gen_ai.usage.prompt_tokens` and
  `gen_ai.usage.completion_tokens` are in no token list, so the token columns
  are 0. The raw keys are still stored as attributes. Adding era A aliases is
  shared processor work (SF-1), not part of this change. The provider column
  is `openai`, read from `gen_ai.system`.
- **Dual-emit is not summed.** `dual_emit` sets the FI name and the OTel
  GenAI name for each count, the pairs traceAI's own note maps one to the
  other (`docs/OTEL_GENAI_SEMANTIC_CONVENTIONS.md:204-205`). Both names are
  aliases. The prompt column reads `llm.token_count.prompt` and stops, so
  tokens are 3 / 4 / 7. Adding every alias present would give 6 / 8 / 14.
- **The era A prompt name is stored, not read.** `dual_emit prompt_tokens`
  sets `gen_ai.usage.input_tokens` and `gen_ai.usage.prompt_tokens`. Only the
  first is an alias, so prompt tokens are 3 whether aliases are added or
  not: this span cannot tell the two apart. If `gen_ai.usage.prompt_tokens`
  became an alias (SF-1), a first-wins lookup would still give 3 and a sum 6.
- **`retrieval` and `invoke_agent` are stored, and no kind is asserted.**
  `gen_ai.operation.name` is the only kind signal on those spans. The
  fixtures set no `fi.span.kind`, `gen_ai.span.kind`, `llm.request.type`,
  `openinference.span.kind` or model key. From reading the source:
  fi-collector maps `chat` to `llm` and stores both of these as `unknown`
  (`exporter/clickhouse25exporter/converter.go:94-131`). Mapping them to a
  kind is SF-1 work.

## Sources that disagree

- The backend's Python adapter
  (`futureagi/tracer/utils/adapters/otel_genai.py`, `4af5338`) prefers the
  response model over the request model and `gen_ai.system` over
  `gen_ai.provider.name` (`:110-119`). fi-collector, which these fixtures
  target, reverses both: it prefers the request model and
  `gen_ai.provider.name`. The Python adapter also falls back to
  `gen_ai.usage.prompt_tokens` and `gen_ai.usage.completion_tokens`
  (`:124-131`). fi-collector does not read `gen_ai.usage.prompt_tokens` at
  all, nor `gen_ai.usage.completion_tokens`: neither key appears in its
  source. That is a missing key, not a reversed order. Neither maps
  `invoke_agent` or `retrieval` (`otel_genai.py:31-38`). This change edits
  neither file.
- traceAI's own note, `docs/OTEL_GENAI_SEMANTIC_CONVENTIONS.md`, lists
  `invoke_agent` and `execute_tool` and does not list `retrieval`
  (`:157-168`). It also maps `fi.span.kind` to `gen_ai.operation.name`
  (`:211`). These fixtures cite the spec repository commit above, not that
  note. This change does not edit the note.

## Tests

`test_fixtures.py` posts each file unchanged with the shared harness
`post_otlp()` to a loopback `Receiver` (`python/tests/harness`) on
`/tracer/v1/traces`. It then checks what the Receiver stored against
`<fixture>.golden.json` with `compare()`. Compared fields are span names,
attributes and status. The Receiver stores what it receives. It does not
authenticate, stamp projects or derive columns.

`columns.golden.json` records the expected columns, the alias lists above
with their line numbers, and the source of `firstString` and `firstNumber`.
`derive_columns()` applies those lists to the stored
attributes the way `DeriveHotKeys` does (`adapter.go:212-235`, `:316-346`),
for the string and integer values these fixtures use. The tests check:

- every span of every fixture is stored and matches its golden;
- the request carries `project_name` and goes to `/tracer/v1/traces`;
- every `traceId` is 32 lowercase hex characters, and every `spanId` and
  `parentSpanId` is 16. fi-collector decodes OTLP/JSON ids as hex of exactly
  that length and answers 400 otherwise, base64 included. The Receiver and
  `compare()` never look at ids. A control shows a base64 span id fails the
  check;
- the columns match `columns.golden.json`;
- `first_number()` takes the first alias present and adds nothing. It
  reads a number before a numeric string for the same key, and skips a
  non-numeric string. `first_string()` skips an empty string;
- dual-emit tokens are 3 / 4 / 7. A control derives the columns by adding
  every alias present instead, gets 6 / 8 / 14, and shows the check fails.
  Another shows that a stored 6 fails both goldens;
- era B's model is `gpt-test`, and era A's token columns are 0;
- no span carries a span-kind key. `retrieval` and `invoke_agent` carry only
  `gen_ai.operation.name`;
- every non-loopback connection is refused. A control shows the
  guard refuses IPv4, IPv6 and DNS attempts, and that `post_otlp()` rejects
  a non-loopback endpoint.

The tests do not run fi-collector. The columns are checked against the
recorded alias lists. The tests that read `adapter.go` are opt-in: set
`FI_COLLECTOR_ADAPTER_GO` to a copy of `fi-collector/pkg/adapter/adapter.go`.
They check that the recorded lists and line numbers equal the file's, and
that `DeriveHotKeys` uses them in the same roles. They also check that the
source of `firstString` and `firstNumber` (`:317-324`, `:330-346`) equals the
copy recorded in `columns.golden.json`. That source is what
`first_string()` and `first_number()` mirror: the first alias present wins,
nothing is added, and for one key a number is read before a numeric string.
A control edits each of those rules in the file's text, keeping every alias
list and line number, and shows the check fails. Without the variable these
tests are skipped. No CI job sets it. Run them whenever `adapter.go`
changes, or the recorded columns may drift from the collector unnoticed.

When this was written, fi-collector's own JSON decoding and span conversion
(`4af5338`) were also run on these three files, outside this repository. That
run produced exactly the columns in `columns.golden.json`. The same decoding
rejected a copy of `era_b.json` with base64 ids ("invalid length for ID").
Neither run is part of these tests.

From the repository root, Python 3.11:

```bash
env -u PYTHONPATH PYTHONPATH="python/tests" \
  uv run --no-project --python 3.11 --with 'pytest==9.1.1' \
  pytest python/tests/fixtures/otel_genai/test_fixtures.py -v \
  -p no:cacheprovider --noconftest -o addopts= -rfEs
```

For Python 3.13, replace `--python 3.11`. Only pytest is needed. The tests
pass on Python 3.10, 3.11, 3.12 and 3.13 with pytest 9.1.1.
`--noconftest` skips `python/tests/conftest.py`, which patches
`fi_instrumentation`; these tests do not use it. With fi-instrumentation
installed (`pip install -e python`), the plain
`pytest python/tests/fixtures/otel_genai/test_fixtures.py` also works.

```bash
git -C <future-agi checkout> show origin/main:fi-collector/pkg/adapter/adapter.go > /tmp/adapter.go
export FI_COLLECTOR_ADAPTER_GO=/tmp/adapter.go   # then run the command above
```

Not covered here: a live instrumentor, `OTEL_SEMCONV_STABILITY_OPT_IN`, auth
(401), project stamping, ClickHouse storage, cost, and the rendered trace.
