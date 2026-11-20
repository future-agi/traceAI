# Zero-code tracing for Python

Zero-code tracing traces a Python AI app without changes to its code. You start the app through one command, or set one environment variable, and every installed traceAI integration turns on the first time the app imports its library. Future AGI receives the AI spans: model calls, agent steps, tool calls and vector database calls, with their inputs, outputs, token usage and timing. Your other OpenTelemetry exporters keep receiving every span.

Zero-code tracing ships in the core package, published on PyPI as `fi-instrumentation-otel` 1.5.1. The full Python guide is [Zero-code tracing for Python](../python/auto-instrumentation.md).

## Requirements

- Python 3.10–3.15 for the core package. Each traceAI package has its own supported library and Python versions.
- An app that uses a supported library (see [What gets traced](#what-gets-traced)).
- A [Future AGI](https://app.futureagi.com) API key and secret key.
- Your model provider's own key, configured as usual.

## Install

Install the core package and the traceAI package for each library your app uses. For an OpenAI app:

```bash
pip install fi-instrumentation-otel traceai-openai
```

To find the traceAI packages your app is missing, run `fi-instrument bootstrap`. To install them, add `--install`:

```bash
fi-instrument bootstrap --install
```

`--all` also covers vector databases and web servers.

## Turn it on

Pick one way to start the app. The app itself needs no tracing imports.

| Start | When to use it |
| --- | --- |
| `fi-instrument python app.py` | Works in any install. Anything after `fi-instrument` runs as given, for example `fi-instrument uvicorn main:app` or `fi-instrument celery -A tasks worker`. `fi-instrument run -- ...` does the same. |
| `FI_AUTO_INSTRUMENT=true python app.py` | Keeps your start command. Every Python process with the variable set is traced, child processes included. Needs a normal `pip install` of the wheel; with an editable or source install, use `fi-instrument`. |
| `FI_AUTO_INSTRUMENT=true opentelemetry-instrument python app.py` | You already start through OpenTelemetry. The `futureagi` entry point starts the agent only when `FI_AUTO_INSTRUMENT` is `true`. Turn it off with `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=futureagi`. |
| `fi_instrumentation.init()` | One line in code, at startup. It takes the same settings as arguments. |

```python
import fi_instrumentation

fi_instrumentation.init(project_name="my-ai-app")
```

`init()` configures the process once. Later calls return the same tracer provider and change nothing.

If the app already calls `register()` and `instrument()` itself, do not also start it with `fi-instrument` or `FI_AUTO_INSTRUMENT`. The agent would register its own provider first, and your calls would have no effect. Remove those calls, or keep them and call `fi_instrumentation.init()` after them.

Spans are exported in batches and flushed when the process exits. For a job or serverless handler that must export before it returns, flush in code:

```python
from fi_instrumentation import auto

auto.flush()      # export buffered spans now
auto.shutdown()   # stop the agent; it cannot be restarted in the same process
```

## Send traces to Future AGI

Set both keys and a project name before the app starts:

```bash
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
export FI_PROJECT_NAME="my-ai-app"
fi-instrument python app.py
```

Without both keys, the app runs untraced and prints one warning. The two keys are read as one pair from one source (the environment, `init()` arguments or `futureagi.toml`). A source that sets only one of them is ignored, with a warning that names the missing one.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Unset | Both are needed to export to Future AGI. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, else the working directory's name | Project in Observe. Set it for stable naming. |
| `FI_PROJECT_TYPE` | `observe` | `observe` or `experiment`. |
| `FI_PROJECT_VERSION_NAME` | Unset | Version name, for `experiment` projects. |
| `FI_BASE_URL` | `https://api.futureagi.com` | Collector base URL for HTTP export. |
| `FI_SAMPLE_RATE` | `1.0` | Fraction of traces to keep, decided per trace. |
| `FI_CONFIG_FILE` | `./futureagi.toml` | Config file to read. A named file must exist. |
| `OTEL_SDK_DISABLED` | `false` | `true` turns tracing off. |

Settings are read in this order: `init()` arguments, then the environment, then `futureagi.toml`, then the defaults.

`fi-instrument init-config` writes a commented `futureagi.toml` template in the current directory. Every key matches an environment variable without its `FI_` prefix (`debug` is `FI_AUTO_DEBUG`). Keep the keys in the environment if you commit the file.

```toml
project_name = "checkout-agent"
project_type = "observe"
disabled_instrumentations = ["bedrock"]
sample_rate = 0.25
hide_inputs = true
```

The file is a flat subset of TOML: one `key = value` per line, with quoted strings, `true`/`false`, numbers or one-line arrays of strings.

Set the collector with `FI_BASE_URL`. A `base_url` in the working directory's `futureagi.toml` is used only when the keys come from the same file, or when you name the file with `FI_CONFIG_FILE` (or `--config`, or `config_file=` in `init()`). When the keys come from elsewhere, export to Future AGI is off, with one warning that starts `traceAI: export to Future AGI is off: futureagi.toml sets base_url`; set `FI_BASE_URL`, put the keys in the same file, or name the file.

## Hide prompt and response content

Prompts, responses and tool content are captured by default. Set these as environment variables or as `futureagi.toml` keys to hide them:

| Environment variable | Config-file key | Hides |
| --- | --- | --- |
| `FI_HIDE_INPUTS` | `hide_inputs` | All inputs |
| `FI_HIDE_OUTPUTS` | `hide_outputs` | All outputs |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES` | `hide_input_messages`, `hide_output_messages` | Chat messages |
| `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | `hide_input_text`, `hide_output_text` | Message text |
| `FI_HIDE_INPUT_IMAGES` | `hide_input_images` | Input images |
| `FI_HIDE_EMBEDDING_VECTORS` | `hide_embedding_vectors` | Embedding vectors |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | `hide_llm_invocation_parameters` | Model parameters |
| `FI_HIDE_ERROR_DETAILS` | `hide_error_details` | Error messages and stack traces |
| `FI_PII_REDACTION` | `pii_redaction` | Pattern-based PII redaction (off by default) |

```bash
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
```

Each switch takes `true`, `1`, `yes` or `on` to turn it on and `false`, `0`, `no` or `off` to turn it off, in any case. An empty value counts as unset. Any other value turns the switch on and logs one warning that names the setting. When `FI_HIDE_ERROR_DETAILS` is unset, it follows the input and output hide settings; set it to `false` to keep error details.

In code, pass `config=TraceConfig(...)` to `init()`.

## What gets traced

Each traceAI integration traces one library. An integration turns on when both the library and its traceAI package are installed, the first time the app imports the library. A library without its traceAI package is not traced.

- **Model providers:** OpenAI (and OpenAI Realtime), Anthropic, Google GenAI (and Live), Google Generative AI, Vertex AI, Bedrock, SageMaker, Mistral, Cohere, Groq, Together, Cerebras, Ollama, Hugging Face, Transformers, LiteLLM, OpenRouter, Writer, Aleph Alpha, Portkey, watsonx, Replicate, Voyage, DeepSeek, Fireworks, xAI, MiniMax, vLLM.
- **Agent frameworks:** LangChain, LangGraph, LlamaIndex, OpenAI Agents, Claude Agent SDK, Google ADK, CrewAI, AutoGen, AG2, DSPy, Haystack, smolagents, Pydantic AI, Instructor, Semantic Kernel, Agno, BeeAI, Agent Framework, Agent Spec, Prompt flow, MCP, A2A, Guardrails, LiveKit, Pipecat.
- **Search and retrieval:** Exa, Firecrawl, Tavily, Parallel, Discovery Engine, Chroma, Pinecone, Qdrant, Weaviate, Elasticsearch, Marqo, Milvus, LanceDB, MongoDB, pgvector, Redis.
- **Web servers:** `fastapi`, `flask`, `django`, `falcon` and `tornado` turn on when `opentelemetry-instrumentation-<name>` is installed. Each incoming request becomes a root span, with its AI calls nested under it. Future AGI receives a request's root span only when that request made an AI call.

Some integrations need a setup call of their own, among them `strands`, `ag2`, `agentscope`, `temporal`, `cognee`, `openwebui` and `mcp_use`. `fi-instrument doctor` reports them as `manual` and shows the call. The full table, with each integration's name, library and package, is in the [Python guide](../python/auto-instrumentation.md#what-is-traced).

The `deepseek`, `fireworks`, `xai`, `minimax` and `vllm` integrations wrap the OpenAI SDK. While `openai` is on, it traces those calls, so each call produces one span.

## Turn integrations off or choose them

Use integration names, comma-separated. The names are listed in the [Python guide](../python/auto-instrumentation.md#what-is-traced) and in `fi-instrument doctor --all`.

| Environment variable | Config-file key | Default | Purpose |
| --- | --- | --- | --- |
| `FI_INSTRUMENTATIONS` | `instrumentations` | Every installed integration | Allowlist, for example `openai,langchain`. |
| `FI_DISABLED_INSTRUMENTATIONS` | `disabled_instrumentations` | None | Denylist. It wins over the allowlist. |

```bash
FI_INSTRUMENTATIONS=openai,langchain fi-instrument python app.py
FI_DISABLED_INSTRUMENTATIONS=bedrock fi-instrument python app.py
```

`init()` takes the same settings as arguments.

## Check that it works

`fi-instrument doctor` shows the settings in effect and where each came from, every installed integration's status, and whether the collector accepts the keys. It shows only the start of `FI_API_KEY` and whether `FI_SECRET_KEY` is set.

```bash
fi-instrument doctor            # add --json, --offline or --all
fi-instrument validate          # check settings and send one empty export; exit 1 on a problem
```

`fi-instrument validate --offline` checks the settings only. `python -m fi_instrumentation.auto` runs the same commands.

Each integration reports one status:

| Status | Meaning |
| --- | --- |
| `instrumented` | Tracing is on. |
| `pending` | Installed; tracing turns on when the app imports the library. |
| `missing_instrumentor` | The library is installed but its traceAI package is not. |
| `manual` | The library needs its own setup call; the detail says which. |
| `disabled` | Turned off by configuration, or off by default. |
| `failed` | Enabling it failed; the detail has the error. The app keeps running. |
| `not_installed` | The library is not installed. Shown by `doctor --all`. |

`doctor` runs as a separate process, so `pending` there means the library is installed, not that your running app imported it. Set `FI_AUTO_DEBUG=true` to print the report from inside the app at startup.

Then open **Observe → my-ai-app** in Future AGI to see the calls.

## Troubleshooting

- **`FI_API_KEY and FI_SECRET_KEY are not set; tracing is off`.** Set both keys in the same place: the environment, `init()` arguments or `futureagi.toml`.
- **A library shows `missing_instrumentor`.** Run `fi-instrument bootstrap --install`, then restart the app.
- **Nothing arrives in Future AGI.** Run `fi-instrument validate` to check `FI_BASE_URL` and the keys. Let the app exit normally, or call `auto.flush()`.
- **`traceAI: tracing is off: ... cannot import fi_instrumentation`.** The Python that runs your app does not have `fi-instrumentation-otel`, for example when `fi-instrument` comes from another environment. Install the package where the app runs.
- **Spans move to another project after adding `fi-instrument`.** Your own `register()` setup no longer applies. Remove it, or call `fi_instrumentation.init()` after it and start the app normally.
- **Duplicate spans, or HTTP and database spans missing in Future AGI.** Remove manual `instrument()` calls for the same library, or turn that integration off with `FI_DISABLED_INSTRUMENTATIONS`. Only AI spans go to Future AGI; your own exporters still receive the rest. A value shown as `<redacted>` or `<omitted: ...>` matched the privacy catalog described in the [Python guide](../python/auto-instrumentation.md#privacy).

## Limits

- Only libraries with an installed traceAI package are traced. Calls made with a plain HTTP client, or through a library without a traceAI package, are not traced in this release.
- Each traceAI package keeps its own supported library versions and Python versions.
- The agent loads at interpreter start, which adds the cost of importing the core package and OpenTelemetry to each traced process.
- `FI_AUTO_INSTRUMENT` needs the installed wheel. With `pip install -e` or `poetry install`, use `fi-instrument`.
- When the agent creates the provider, its resource carries the Future AGI project settings but no `service.name`, and `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` are not read into it.
- With `transport=Transport.GRPC`, spans go to `FI_GRPC_URL`; `FI_BASE_URL` and `base_url` apply to HTTP export only.
- Integrations whose package converts spans in its own exporter wrapper, such as `strands`, are manual.

## More detail

- [Zero-code tracing for Python](../python/auto-instrumentation.md): the full guide, including existing OpenTelemetry setups, privacy and shutdown.
- [traceAI for Python](../../python/README.md): the package README, with manual setup and the complete package catalog.
