# Crewai OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Crewai framework. It enables tracing and monitoring of applications built with Crewai.

## Installation

1. **Install traceAI Crewai**

```bash
pip install traceAI-crewai
```

2. **Install Crewai and Crewai_tools**

```bash
pip install crewai crewai_tools
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY

# Set these before importing CrewAI to opt out of its separate native telemetry.
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_DISABLE_TRACKING"] = "true"
```

### Privacy and credentials

#### Structural guarantee: framework configuration is not content

The instrumentor never exports an LLM, Agent, Crew, Task, Tool or client object
itself, and never calls its `repr`, `str`, `model_dump`, `dict`, `vars` or generic
JSON serializer to capture configuration. LLM API keys, base URLs (including
URLs with userinfo), configuration headers and credentials are not selected.
This does not depend on finding a secret's spelling or value.

The complete wrapper inventory is:

| Wrapper | Explicit input projection | Explicit result projection |
| --- | --- | --- |
| `Task._execute_core`, `Task._aexecute_core` | Task description, expected output and application context; no agent or tool argument object | `TaskOutput.json_dict` when it is a plain dictionary, otherwise `TaskOutput.raw`; plain application results |
| `Crew.kickoff`, `Crew.akickoff` | Application `inputs`; named agent/task business metadata below | `CrewOutput.json_dict` when it is a plain dictionary, otherwise `CrewOutput.raw`; crew-level prompt/completion/total usage roll-up |
| `ToolUsage._use` | `ToolCalling.tool_name`, `ToolCalling.arguments`; tool name only from the runtime tool | Plain application result; no tool configuration |
| `CrewStructuredTool.invoke`, `CrewStructuredTool.ainvoke` | Tool name and application `input`; no config, callbacks or schema | Plain application result |
| `BaseTool.run`, `BaseTool.arun`, `Tool.run`, `Tool.arun` | Tool name and application arguments bound to the tool callable; no callable/configuration serialization | Plain application result |
| `Agent.kickoff`, `Agent.kickoff_async` | Application `messages`; no LLM configuration, response schema, files or checkpoints | `LiteAgentOutput.raw`; no cumulative usage counters |
| `Flow.kickoff_async` (also covers `Flow.kickoff` and `Flow.akickoff`) | Explicit application `inputs`; no Flow state, memory, files or checkpoints | Plain application result, or the same explicit Crew/Task/Agent result projection above |

Optional hooks are installed only when the installed CrewAI exposes them;
standalone Agent hooks target CrewAI 1.15+ and its coroutine-factory contract. The
legacy CrewAI 0.41.1 floor retains Crew/Task/ToolUsage coverage without importing
unavailable Agent/Flow APIs. `Crew.kickoff_async` delegates to `Crew.kickoff`;
`Agent.akickoff` delegates to `Agent.kickoff_async`. They are not wrapped twice.
Flow's defining `kickoff_async` method is wrapped once, including when inherited.
Private executor Flows marked `is_crewai_internal` are omitted, while nested user
Flows remain separate child spans. Tool delegates deduplicate the same tool only.
All hooks honor OTel suppression and are restored on `uninstrument()`.

#### Streaming and async lifecycle

Crew and Flow streaming factories are lazy. They emit no execution span until
consumed; the native worker calls the non-streaming entry point, so the span uses
the **consuming context**, not the context where the stream object was created.
The adapter preserves native iterators, cancellation and close behavior, and
records the final result/usage on the execution span rather than serializing a
stream object. An unconsumed factory emits no span. Async spans remain open until
the operation completes, fails or is cancelled. When `Agent.kickoff` returns a
coroutine inside a Flow/event loop, the awaited `Agent.kickoff_async` execution
owns its span; creating the coroutine alone is not a second execution.
Cancellation does not forcibly stop native worker threads: if an Agent's tool is
already running in a thread, cancelling the Agent ends its span with ERROR,
while the tool span stays open until that worker actually returns or fails.
Applications must still arrange for long-running tools to stop or finish.

Crew metadata selects only crew/task keys and UUIDs; agent role, goal, backstory,
key, UUID, verbosity, iteration/rate limits, delegation flag and tool names;
and task description, expected output, UUID, execution/human-input flags,
assigned agent role/key, context task descriptions and tool names. Task description
and expected output are also emitted as formatted aliases when `share_crew=True`.
`Agent.execute_task` has no additional wrapper: it executes inside the task span.
LLM calls are owned by a separately installed maintained provider instrumentor,
not a second CrewAI LLM wrapper or event listener. Follow the composition recipe
below for per-call model, provider, messages and usage. Provider instrumentors
have their own projections; the CrewAI-specific field-name policy does not
implicitly apply to those other adapters.

Opaque runtime objects nested in application data are replaced with
`[omitted runtime object]`, without inspecting their attributes. The guarantee is
about framework configuration objects, not credentials deliberately copied into
application prose or a plain dictionary. Instrumentation preserves application
arguments, results and raised exceptions.

#### Application content: a field-name policy, not a universal secret detector

With content capture on, task/tool inputs and outputs are application content.
The following policy applies to plain dictionaries, lists and tuples (copied
without mutation), and complete JSON object/array strings. It also applies to
context and span/event attributes, including structured `log.body` attributes.
It does not filter independent application logging pipelines or console output.

Names are case-insensitive; an initial `X-`/`X_` prefix and punctuation/separators
are normalized away. Dot/bracket field paths are inspected component by
component, including nested dictionary paths. A field and all its descendants
are replaced by `__REDACTED__` if the normalized component contains any of these
markers:

- `apikey`, `accesskey`, `privatekey`, `clientkey`, `signingkey`,
  `subscriptionkey`, `applicationkey`, `masterkey`;
- `token`, `secret`, `password`, `passwd`, `passphrase`, `credential`, `auth`,
  `cookie`, `session`, `signature`, `bearer`, `jwt`, `otp`.

`pin` matches a whole normalized component only. Adjacent components are also
combined for the eight `*key` markers above, so `client.api.key` and
`client[private][key]` match their single-field equivalents.

Header interpretation requires an explicit field whose normalized name ends in
`headers` (for example `headers`, `request_headers`, `rawHeaders`):

- Dictionaries with credential-named header keys;
- Two-item name/value pairs and collections of pairs;
- Alternating name/value lists such as Node's `rawHeaders`;
- Records with a `name`, `key` or `header` selector and a `value` field;
- Flattened dot/bracket records with those selectors, or `0`/`1` selectors,
  including descendants of list-valued `value` fields in one attribute batch.

A credential-named header selector preserves its name but redacts the entire
paired value. Outside a named headers field, pairs and `{name, value}` records
are ordinary business data, never inferred to be headers. For example,
`topics=['authentication', 'billing']`, business tables and product records keep
their values.

The exact normalized metadata exceptions below remain visible (children are
still inspected). These cover standard token counters, generation limits,
tokenizer metadata, author metadata and non-secret session/service metadata:

`acceptedpredictiontokens`, `apisecretid`, `audiotokens`, `author`, `authoremail`;
`authorid`, `authority`, `authorname`, `authors`, `authorship`;
`authorurl`, `cachecreationinputtokens`, `cachedcontenttokencount`, `cachedinputtokens`, `cachedtokencount`;
`cachedtokens`, `cachereadinputtokens`, `cachereadtokens`, `cachewriteinputtokens`, `cachewritetokens`;
`candidatestokencount`, `completiontokencount`, `completiontokens`, `completiontokensdetails`, `consultokenttl`;
`inputtokencount`, `inputtokens`, `inputtokensdetails`, `maxcompletiontokens`, `maxinputtokens`;
`maxnewtokens`, `maxoutputtokens`, `maxtokens`, `numtokens`, `outputtokencount`;
`outputtokens`, `outputtokensdetails`, `promptcachehittokens`, `promptcachemisstokens`, `prompttokencount`;
`prompttokens`, `prompttokensdetails`, `reasoningtokencount`, `reasoningtokens`, `rejectedpredictiontokens`;
`securitytokenid`, `servicetokencount`, `sessioncount`, `sessionduration`, `sessiondurationms`;
`subjecttokentype`, `thoughtstokencount`, `tokencount`, `tokenid`, `tokenizer`;
`tokenizername`, `tokenizerversion`, `tokensused`, `tokenusage`, `tooluseprompttokencount`;
`totaltokencount`, `totaltokens`, `vaulttokencount`.

These are field-name rules, not value inspection. Unknown credential names
(for example an application-specific `pwd`, `sig` or vendor key header not
matching the markers), novel shapes, arbitrary free text and encodings other
than complete JSON objects/arrays are outside this policy. Do not put credentials
in content fields or metadata. Dictionaries beyond depth 20 are omitted, as are
structured strings larger than 1,000,000 characters or unparseable structured
strings; opaque objects are not dumped as a fallback.

To disable content capture, set `FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true`
before instrumentation, or pass `TraceConfig(hide_inputs=True, hide_outputs=True)`.
Directional text/message hiding also covers CrewAI's content aliases. Wrapper
failures record ERROR status without copying exception messages or stacktraces.
For manually recorded error events, use `FI_HIDE_ERROR_DETAILS=true`.

### CrewAI's separate native telemetry

CrewAI can export through its own provider to `telemetry.crewai.com`, independently
of traceAI. **This instrumentor does not change environment variables, patch
CrewAI's telemetry classes, disable its provider or restore/change your settings
on `uninstrument()`.** Choose the native telemetry policy explicitly before
importing CrewAI.

In CrewAI 0.152.0 and 1.15.33, `CREWAI_DISABLE_TELEMETRY=true` or
`CREWAI_DISABLE_TRACKING=true` disables native telemetry while leaving traceAI
available. `OTEL_SDK_DISABLED=true` is also honored, but disables the OTel SDK
and traceAI spans too; do not use it when you want traceAI tracing.
`CREWAI_TELEMETRY_OPT_OUT` and `DO_NOT_TRACK` are not native disable flags.

CrewAI 0.41.1 has no native telemetry opt-out flag: its constructor creates an
exporter for `https://telemetry.crewai.com:4319/v1/traces` and ignores the flags
above. If you need native telemetry disabled, upgrade to a version that honors
`CREWAI_DISABLE_TELEMETRY` before running sensitive workloads. For legacy
workloads, enforce a network egress policy; traceAI's projections cannot protect
an independent provider's exports. `share_crew=False` is not a native telemetry
disable switch.

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="crewai_app"
)
```

### Configure Crewai Instrumentation
Instrument CrewAI to trace the framework entry points listed above.

```python
from traceai_crewai import CrewAIInstrumentor

CrewAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Per-call LLM attribution with OpenAI

Install and enable the maintained provider adapter **as well as** the CrewAI
adapter. This covers CrewAI's native OpenAI client and the `ChatOpenAI` client
used by CrewAI 0.41.1, including streamed responses. It does not require a second
LangChain or LiteLLM instrumentor for those OpenAI calls.

```bash
pip install traceAI-crewai 'traceAI-openai>=0.1.12'
```

Use the same provider and privacy configuration for both adapters, before
executing any crews, agents or flows. This replaces the framework-only
instrumentation call above, rather than registering it a second time.

```python
from fi_instrumentation import TraceConfig
from traceai_crewai import CrewAIInstrumentor
from traceai_openai import OpenAIInstrumentor

# Privacy-first: model/provider/usage remain visible, content does not.
config = TraceConfig(
    hide_inputs=True,
    hide_outputs=True,
    hide_error_details=True,
)
CrewAIInstrumentor().instrument(tracer_provider=trace_provider, config=config)
OpenAIInstrumentor().instrument(tracer_provider=trace_provider, config=config)
```

To capture application messages/results, explicitly set `hide_inputs=False` and
`hide_outputs=False` for both adapters; never put credentials in those fields.
The composition yields Crew/Task (or standalone Agent) parents and one LLM span
per SDK call, with `gen_ai.request.model`, `gen_ai.provider.name`, input/output,
and provider-reported `gen_ai.usage.input_tokens`, `output_tokens` and
`total_tokens` when returned by the provider. Streaming usage requires usage
chunks; on legacy `ChatOpenAI`, use `stream_usage=True`. For CrewAI 0.152.0's
intermediate LiteLLM transport, this OpenAI composition does not expose streamed
per-call usage: use non-streaming calls for usage accounting, or upgrade to the
native OpenAI route in CrewAI 1.15+.

For mixed-model accounting, **sum only LLM spans**. Crew usage is a compatibility
roll-up, not an additional charge; legacy CrewAI can estimate it using its own
tokenizer. Standalone Agent/Flow spans deliberately do not copy cumulative LLM
usage. Do not enable overlapping OpenAI, LangChain, LiteLLM or event-based LLM
adapters for the same SDK call: that would duplicate spans and token totals.
Other provider SDKs need their corresponding maintained provider adapter;
OpenAI composition does not claim coverage for every CrewAI LLM transport.

To remove the composition, call both `CrewAIInstrumentor().uninstrument()` and
`OpenAIInstrumentor().uninstrument()`. Removing the framework adapter alone does
not disable independently configured provider tracing.

### Create Crewai Components
Set up your Crewai client with built-in observability.

```python
from crewai import LLM, Agent, Crew, Process, Task

def story_example():
    llm = LLM(
        model="gpt-4",
        temperature=0.8,
        max_tokens=150,
        top_p=0.9,
        frequency_penalty=0.1,
        presence_penalty=0.1,
        stop=["END"],
        seed=42,
    )

    writer = Agent(
        role="Writer",
        goal="Write creative stories",
        backstory="You are a creative writer with a passion for storytelling",
        allow_delegation=False,
        llm=llm,
    )

    writing_task = Task(
        description="Write a short story about a magical forest",
        agent=writer,
        expected_output="A short story about a magical forest",
    )

    crew = Crew(agents=[writer], tasks=[writing_task])

    # Execute the crew
    result = crew.kickoff()
    print(result)

if __name__ == "__main__":
    story_example()
```

