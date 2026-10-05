# Gradio chat → Future AGI (recipe)

There is no `traceai-gradio` package. Do not install one. Gradio emits no
GenAI spans: no Python module in the `gradio` 6.29.1 wheel imports
OpenTelemetry. The span you see comes from `traceAI-openai` (import
`traceai_openai`) instrumenting the OpenAI client that the chat function
calls. With that instrumentor removed, the same Gradio turn exports zero
spans (tested).

Pinned: `gradio==6.29.1`, the latest release on PyPI on 2026-10-05
(`Requires-Python: >=3.10`). Its wheel METADATA declares
`License-Expression: Apache-2.0` and ships a `LICENSE` file. It is a
dependency of this example only, not of any traceAI package. `openai` is
pinned to 3.24.0, the version the tests ran with.

The tests (see [Tests](#tests)) pass on Python 3.11.12 and 3.13.15 against
loopback fakes only: no model key, no live Future AGI project. Python 3.10
and 3.12 are within Gradio's metadata but were not tested here;
`fi-instrumentation-otel` and `traceAI-openai` declare Python <3.14.
Statements marked "source reading" come from the pinned code and are not
exercised by the tests.

## Run

```bash
cd python/examples/gradio
pip install -r requirements.txt

export FI_API_KEY="YOUR_API_KEY"         # sent as the X-Api-Key header
export FI_SECRET_KEY="YOUR_SECRET_KEY"   # sent as the X-Secret-Key header
export FI_PROJECT_NAME="my-chatbot"
export OPENAI_API_KEY="YOUR_OPENAI_KEY"  # optional: OPENAI_BASE_URL, OPENAI_MODEL

python src/app.py
```

Open the URL Gradio prints and chat. Each turn is one LLM span in the
`my-chatbot` project. `OPENAI_BASE_URL` points the client at any
OpenAI-compatible host; the tests use a loopback one.

## The recipe

[`src/app.py`](src/app.py), in full apart from imports, `MODEL`, docstrings
and comments:

```python
def init_tracing(trace_content: bool = False):
    provider = register(
        project_name=os.environ["FI_PROJECT_NAME"],
        project_type=ProjectType.OBSERVE,
        verbose=False,
    )
    if trace_content:
        config = TraceConfig()
    else:
        config = TraceConfig(hide_inputs=True, hide_outputs=True)
    OpenAIInstrumentor().instrument(tracer_provider=provider, config=config)
    return provider


client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(part["text"] for part in content if part.get("type") == "text")


def predict(message: str, history: list, request: gr.Request = None) -> str:
    messages = [{"role": m["role"], "content": _text(m["content"])} for m in history]
    messages.append({"role": "user", "content": message})
    session_id = request.session_hash if request is not None else None
    with using_session(session_id) if session_id else nullcontext():
        response = client.chat.completions.create(model=MODEL, messages=messages)
    return response.choices[0].message.content or ""


demo = gr.ChatInterface(predict, analytics_enabled=False)

if __name__ == "__main__":
    init_tracing()
    demo.launch()
```

- **`register()` at startup.** It exports OTLP/HTTP protobuf to
  `<FI_BASE_URL>/tracer/v1/traces` (default origin
  `https://api.futureagi.com`) with the `X-Api-Key` and `X-Secret-Key`
  headers, and stamps every batch with the resource attributes
  `project_name` and `project_type=observe`. fi-collector rejects a batch
  whose resource has no `project_name` (source reading). Spans are batched;
  `register()` flushes the batch on normal exit (tested: with a 10-minute
  batch delay every span still arrived) and on SIGINT/SIGTERM, so Ctrl+C on
  `python src/app.py` flushes too (source reading).
- **Content off.** traceAI records prompts and answers by default
  (`TraceConfig`'s `hide_inputs` and `hide_outputs` default to `False`). The
  recipe passes `hide_inputs=True, hide_outputs=True`. See [Privacy](#privacy).
- **Session.** Gradio passes a `gr.Request` to any chat function that
  declares one. Its `session_hash` identifies one browser session (one page
  load), not a user, and it is not a Future AGI user id. Passed to
  `using_session`, it puts the same `session.id` on every turn of that
  session. Without it (`predict` called with no request), turns carry no
  session id.
- **History.** Gradio 6 passes each history message's `content` as a list of
  parts (`[{"type": "text", "text": ...}]`). `_text` turns it back into the
  plain string the Chat Completions API takes (tested with Gradio's own
  history round trip).
- **`analytics_enabled=False`** turns off Gradio's own usage analytics.
  Without it, building the app contacts `api.gradio.app` (tested). This is
  unrelated to tracing.
- **Order.** The client is created at import, before `init_tracing()`. That
  works: the instrumentor wraps `OpenAI.request` on the class, so existing
  clients are traced too (tested).
- **No `print`.** Nothing prints or logs the user's message (tested).

## What is traced

One span per chat turn, from `traceai_openai`:

| Span | Parent | Attributes |
|---|---|---|
| `ChatCompletion` | none: each turn is its own trace | `gen_ai.span.kind=LLM`, `gen_ai.provider.name=openai`, `gen_ai.request.model`, `gen_ai.request.parameters`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens`, `gen_ai.tool.definitions` (`null` here), `input.value` and `output.value` (both `__REDACTED__`), and, with a Gradio session, `session.id` and `gen_ai.conversation.id` |

- fi-collector reads the span kind from `gen_ai.span.kind` and stores this
  span as `llm`. It promotes `gen_ai.request.model`, `gen_ai.provider.name`
  and the `gen_ai.usage.*` token counts, and reads `session.id` for the
  session. (Source reading of fi-collector; the tests use the harness
  receiver, not fi-collector.)
- `gen_ai.request.model` holds the model the response names
  (`gpt-4o-mini-2024-07-18` from the test fake), not the requested
  `gpt-4o-mini`; the requested one is in `gen_ai.request.parameters`.
- `openai` is the OpenAI instrumentor's provider label, not Gradio's. It was
  `openai` for the loopback test host too.

## Privacy

With the recipe as written, no message text reaches Future AGI:
`input.value` and `output.value` are `__REDACTED__`, and there are no
`gen_ai.input.messages.*` or `gen_ai.output.messages.*` keys. The test puts
markers in the user's message and the model's answer and finds neither in
the export.

To turn content on, call `init_tracing(trace_content=True)`. **Warning:** the
span then carries the whole conversation that `predict` sends, on every turn:
`gen_ai.input.messages.<i>.message.role` and `.content` for each history
message and the new one, `gen_ai.output.messages.0.message.*` and
`output.value` for the answer, and `input.value`, which holds the text of the
request's first message (on later turns, an earlier user message, not the
latest). Tested. With content on, traceAI's `FI_HIDE_INPUTS` and
`FI_HIDE_OUTPUTS` environment variables apply again (source reading,
`fi_instrumentation/instrumentation/config.py`).

Not covered by the switch (source reading of
`traceai_openai/_request.py`): when the model call raises, the span records
the exception type and message as an `exception` event and in the status
description.

No Future AGI key, secret or OpenAI key appears in any exported span or
resource, or in the app's output (tested). The keys travel only as request
headers: the Future AGI keys to fi-collector, the OpenAI key to the model
host.

## Gradio's thread pool and a turn span

The recipe adds no span of its own, so the OpenAI instrumentor is the only
source. If you want one span per turn around the model call, wrap `predict`
with traceAI's manual API:

```python
from fi_instrumentation import FITracer

tracer = FITracer(provider.get_tracer(__name__))  # provider = init_tracing()


def predict_in_a_turn(message, history, request: gr.Request = None):
    with tracer.start_as_current_span("chat_turn", fi_span_kind="chain"):
        return predict(message, history, request)
```

Does the LLM span stay under it when Gradio runs the function? Tested
through Gradio 6.29.1's own event dispatch (the ChatInterface submit event,
called with `Blocks.call_function`; no server):

- **Plain function: yes.** Gradio runs a sync chat function in an anyio
  worker thread (`gradio/chat_interface.py:940`), and anyio 4.15.1 runs it
  in a copy of the caller's context (`anyio/_backends/_asyncio.py:2698`,
  `:1100`). A turn span opened inside the function, or around the dispatch
  on the event loop, is the LLM span's parent, in one trace. No context
  bridge is needed, and none is added.
- **Generator function: no, after the first `yield`.** Gradio steps a sync
  generator one item per worker call (`gradio/utils.py:897-910`), each in a
  fresh copy of the event loop's context. A span opened inside the
  generator is current only until the first `yield`. A model call made
  after it becomes a root span in another trace, and OpenTelemetry logs
  `Failed to detach context` when the `with` block closes. The recipe does
  not stream. If you turn `predict` into a generator, make the model call
  before the first `yield` or do not wrap the generator in a turn span.
  traceAI has no context-bridge helper for this case.

Streaming responses (`stream=True`) are the OpenAI instrumentor's existing
behaviour (`python/frameworks/openai/traceai_openai/_stream.py`); this recipe
does not test them.

## Gradio reload mode

`gradio src/app.py` (reload mode) does not restart the process when you
save. Gradio 6.29.1 runs the file again in the same process, on a watcher
thread (`gradio/utils.py`, `watchfn`), so `init_tracing()` runs again:

- `register()` builds another provider with its own batch queue, and
  `OpenAIInstrumentor().instrument()` is ignored with the warning
  `Attempting to instrument while already instrumented`. Spans keep going to
  the first provider, with the first `TraceConfig`.
- **Edits to the tracing setup do not take effect until you restart.** In
  the tested replay, an edit that turned content on was saved and reloaded,
  and the next turn still exported no content.
- In that replay no span was dropped: the turns before and after the reload
  were exported at exit by the first provider.

A batch is lost only if the process ends without the exit flush.
`register()` flushes on normal exit, SIGINT and SIGTERM. SIGKILL, or the
SIGHUP a closed terminal sends, skips it (source reading of
`fi_instrumentation/otel.py`, `setup_signal_handlers`; not tested). Each save
also leaves one idle provider behind until the process exits (source
reading).

The replay re-runs `src/app.py` the way `watchfn` does. It is not a
`gradio src/app.py` run: the tests open no server port.

## Troubleshooting

- **No span.** `init_tracing()` did not run. Here it runs under
  `if __name__ == "__main__":`; if you import `demo` from another module
  (for example to mount it in FastAPI), call `init_tracing()` there. Gradio
  will not emit the span for you.
- **A span labelled Gradio.** There isn't one. The span comes from
  `traceai_openai`.
- **Turns not grouped by session.** `predict` ran without a `gr.Request`,
  so no session id was set.
- **A tracing change did nothing after a save.** Reload mode keeps the
  first instrumentation. Restart the process.

## Out of scope

`gradio[mcp]`, Gradio's queue metrics and Spaces hosting. Not tested here:
Gradio's HTTP and queue layer (the tests call its event dispatch directly),
streaming, model errors, and a real fi-collector (authentication, project
stamping and storage).

## Tests

`tests/test_gradio_recipe.py` has two parts. Neither opens a browser or a
Gradio server port.

- **In-process.** `src/app.py` is imported and `register` is swapped for a
  provider with an `InMemorySpanExporter`; the recipe's own `init_tracing()`
  installs the instrumentor. The tests call `predict` directly and through
  Gradio's event dispatch: one LLM span per turn, the session id only when
  the turn has a Gradio session, zero spans with the instrumentor removed
  (with that provider also set as the global one), Gradio's history format,
  and the turn-span results above.
- **Contract.** `tests/drive_turns.py` runs two turns with the real
  `register()` in a subprocess. `tests/_guarded_run.py` blocks and logs
  every non-loopback connection; a positive control
  (`tests/_guard_probe.py`) proves it refuses and logs IPv4, IPv6 and DNS
  attempts. The model is a loopback fake of the Chat Completions API
  (`tests/_fake_openai.py`), and spans go to the shared harness `Receiver`
  (`python/tests/harness`), which serves `/v1/traces` and
  `/tracer/v1/traces` like fi-collector but does not authenticate or store
  anything. The tests check the request path, both key headers, the
  resource (`project_name`, `project_type=observe`), the exit flush, the
  span attributes, content off and (as a control) on, that no key reaches
  the export or the output, that the prompt is never printed, the reload
  replay and the Gradio analytics probe (`tests/_analytics_probe.py`).

All keys are placeholders. From the repository root, Python 3.11:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'pytest==9.1.1' --with 'gradio==6.29.1' --with 'anyio==4.15.1' \
  --with 'openai==3.24.0' --with 'opentelemetry-api==1.45.0' \
  --with 'opentelemetry-sdk==1.45.0' \
  --with 'opentelemetry-exporter-otlp-proto-http==1.45.0' \
  --with 'opentelemetry-proto==1.45.0' \
  --with 'opentelemetry-instrumentation==0.66b0' --with 'wrapt==1.17.3' \
  --with 'requests==2.34.2' --with 'jsonschema==4.26.0' \
  --with 'pydantic==2.13.5' --with 'protobuf==7.36.2' \
  pytest python/examples/gradio/tests -q -p no:cacheprovider \
  --noconftest -o addopts= -rfEs
```

For Python 3.13, replace `--python 3.11`. A full run takes under a minute.
The thread-pool results depend on anyio, so it is pinned with Gradio.

`register()` and the instrumentor come from this repository's
`python/fi_instrumentation` and `python/frameworks/openai`, not from the
releases pinned in `requirements.txt`. The published `traceAI-openai` 0.1.10
wheel's `traceai_openai` package is byte-identical to
`python/frameworks/openai/traceai_openai`. The published
`fi-instrumentation-otel` 1.1.0 differs from `python/fi_instrumentation`
only in shutdown handling (log calls instead of `print`, a lock, and its
exporters also calling the base `shutdown()`) and one extra provider enum
value.

No CI job runs this example. Re-run the command above when Gradio, anyio or
the OpenAI instrumentor changes.
