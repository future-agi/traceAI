# Zero-code tracing for Python

**Trace a Python AI app without changing its code.** Start it with one command, and every installed traceAI integration turns on when its library is imported.

[Quick start](#quick-start) · [Ways to start](#ways-to-start) · [What is traced](#what-is-traced) · [Configuration](#configuration) · [Privacy](#privacy) · [Existing OpenTelemetry setup](#existing-opentelemetry-setup) · [Commands](#commands) · [Troubleshooting](#troubleshooting)

## Quick start

You need Python 3.10 or later, an app that uses a supported library, and a [Future AGI](https://app.futureagi.com) API key and secret key. Keep your model provider's own key configured as usual.

Install the core package and the traceAI package for the library your app uses, for example OpenAI:

```bash
pip install fi-instrumentation-otel traceai-openai
```

Set both Future AGI keys and a project name, then start the app through `fi-instrument`:

```bash
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
export FI_PROJECT_NAME="my-ai-app"
fi-instrument python app.py
```

The app itself has **no tracing imports**. For example, with `OPENAI_API_KEY` and `OPENAI_MODEL` set:

```python
import os
from openai import OpenAI

response = OpenAI().chat.completions.create(
    model=os.environ["OPENAI_MODEL"],
    messages=[{"role": "user", "content": "Say hello in one word."}],
)
print(response.choices[0].message.content)
```

Open **Observe → my-ai-app** in Future AGI to see the call with its model, input, output, token usage and timing. Spans are sent when the app exits. **Prompts and responses are captured by default**; see [Privacy](#privacy) to hide them.

## Ways to start

| Start | When to use it |
| --- | --- |
| `fi-instrument python app.py` | Works in any install. Anything after `fi-instrument` is run as given: `fi-instrument uvicorn main:app`, `fi-instrument celery -A tasks worker`. |
| `FI_AUTO_INSTRUMENT=true python app.py` | Keep your exact start command. Every Python process with the variable set is traced, including child processes. Needs the installed wheel (a normal `pip install`); editable or source installs use `fi-instrument`. |
| `opentelemetry-instrument python app.py` | You already start through OpenTelemetry. The `futureagi` entry point exports only when Future AGI keys or a `futureagi.toml` are present. Turn it off with `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=futureagi`. |
| `fi_instrumentation.init()` | One line in code, at startup. It takes the same settings as arguments. |

```python
import fi_instrumentation

fi_instrumentation.init(project_name="my-ai-app")
```

`init()` configures the process once. Later calls return the same tracer provider and change nothing. Libraries imported before `init()` are traced from then on; libraries imported later are traced when they are imported.

## What is traced

Each traceAI integration traces one library. When your app runs, every integration whose library **and** traceAI package are installed is switched on the first time the library is imported, so unused libraries cost nothing at startup.

A library whose traceAI package is not installed is **not traced** in this release: there is no capture at the HTTP level yet. `fi-instrument doctor` lists such libraries as `missing_instrumentor`, and `fi-instrument bootstrap --install` installs the missing packages.

Each integration reports one of these statuses:

| Status | Meaning |
| --- | --- |
| `instrumented` | Tracing is on. |
| `pending` | Installed; tracing turns on when the app imports the library. |
| `missing_instrumentor` | The library is installed but its traceAI package is not, so it is not traced. |
| `manual` | The library needs its own setup call; the detail says which. |
| `disabled` | Turned off by configuration, or off by default (the detail says why). |
| `failed` | Enabling it failed; the detail has the error. The app keeps running. |
| `not_installed` | The library is not installed. Shown by `doctor --all`. |

<details>
<summary>All integrations</summary>

**Automatic** integrations turn on by themselves. **Off by default** integrations wrap the OpenAI SDK, so the `openai` integration already traces the same calls; to use one instead, list the integrations you want in `FI_INSTRUMENTATIONS` and leave `openai` out. **Manual** integrations need the setup call shown in the report.

| Name | Library | traceAI package | Setup |
| --- | --- | --- | --- |
| `openai` | `openai` | `traceAI-openai` | Automatic |
| `openai_realtime` | `openai` (Realtime) | `traceAI-openai-realtime` | Automatic |
| `anthropic` | `anthropic` | `traceAI-anthropic` | Automatic |
| `google_genai` | `google-genai` | `traceAI-google-genai` | Automatic |
| `google_genai_live` | `google-genai` (Live) | `traceAI-google-genai-live` | Automatic |
| `vertexai` | `vertexai` | `traceAI-vertexai` | Automatic |
| `bedrock` | `boto3` | `traceAI-bedrock` | Automatic |
| `mistralai` | `mistralai` | `traceAI-mistralai` | Automatic |
| `cohere` | `cohere` | `traceAI-cohere` | Automatic |
| `groq` | `groq` | `traceAI-groq` | Automatic |
| `together` | `together` | `traceAI-together` | Automatic |
| `cerebras` | `cerebras-cloud-sdk` | `traceAI-cerebras` | Automatic |
| `ollama` | `ollama` | `traceAI-ollama` | Automatic |
| `huggingface` | `huggingface_hub` | `traceAI-huggingface` | Automatic |
| `transformers` | `transformers` | `traceAI-transformers` | Automatic |
| `litellm` | `litellm` | `traceAI-litellm` | Automatic |
| `openrouter` | `openrouter` | `traceAI-openrouter` | Automatic |
| `portkey` | `portkey-ai` | `traceAI-portkey` | Automatic |
| `replicate` | `replicate` | `traceAI-replicate` | Automatic |
| `voyage` | `voyageai` | `traceAI-voyage` | Automatic |
| `discoveryengine` | `google-cloud-discoveryengine` | `traceAI-discoveryengine` | Automatic |
| `typesafe` | `typesafe_sdk` | `traceAI-typesafe` | Automatic |
| `deepseek`, `fireworks`, `xai`, `minimax`, `vllm` | `openai` | `traceAI-deepseek`, `traceAI-fireworks`, `traceAI-xai`, `traceAI-minimax`, `traceAI-vllm` | Off by default |
| `langchain` | `langchain-core` | `traceAI-langchain` | Automatic |
| `langgraph` | `langgraph` | `traceAI-langchain` | Automatic |
| `llama_index` | `llama-index` | `traceAI-llamaindex` | Automatic |
| `openai_agents` | `openai-agents` | `traceAI-openai-agents` | Automatic |
| `claude_agent_sdk` | `claude-agent-sdk` | `traceai-claude-agent-sdk` | Automatic |
| `google_adk` | `google-adk` | `traceAI-google-adk` | Automatic |
| `crewai` | `crewai` | `traceAI-crewai` | Automatic |
| `autogen` | `autogen-agentchat` | `traceAI-autogen` | Automatic |
| `dspy` | `dspy` | `traceAI-DSPy` | Automatic |
| `haystack` | `haystack-ai` | `traceAI-haystack` | Automatic |
| `smolagents` | `smolagents` | `traceAI-smolagents` | Automatic |
| `pydantic_ai` | `pydantic-ai` | `traceai-pydantic-ai` | Automatic |
| `instructor` | `instructor` | `traceAI-instructor` | Automatic |
| `semantic_kernel` | `semantic-kernel` | `traceAI-semantic-kernel` | Automatic |
| `strands` | `strands-agents` | `traceai-strands` | Automatic |
| `agno` | `agno` | `traceai-agno` | Automatic |
| `beeai` | `beeai-framework` | `traceai-beeai` | Automatic |
| `agent_framework` | `agent-framework` | `traceAI-agent-framework` | Automatic |
| `ag2_classic` | `ag2` (`autogen`) | `traceAI-ag2-classic` | Automatic |
| `ag2` | `ag2` (`autogen.beta`) | `traceAI-ag2` | Manual: `traceai_ag2.setup(agent, ...)` |
| `agentscope` | `agentscope` | `traceAI-agentscope` | Manual: `traceai_agentscope.configure(project_name=...)` and `TracingMiddleware()` |
| `temporal` | `temporalio` | `traceAI-temporal` | Manual: `traceai_temporal.configure()` before `Client.connect()`; start the app without the agent |
| `cognee` | `cognee` | `traceai-cognee` | Manual: `traceai_cognee.configure(project_name=...)` |
| `openwebui` | `open-webui` | `traceAI-openwebui` | Manual: enable its Pipe Function in Open WebUI |
| `mcp` | `mcp` | `traceAI-mcp` | Automatic |
| `mcp_use` | `mcp-use` | `traceAI-mcp-use` | Manual: pass `FutureAGICallback()` to your `MCPAgent` |
| `a2a` | `a2a-sdk` | `traceAI-a2a` | Automatic |
| `guardrails` | `guardrails-ai` | `traceAI-guardrails` | Automatic |
| `hermes_agent` | Hermes Agent | `traceAI-hermes-agent` | Loaded by Hermes as a plugin |
| `livekit` | `livekit-agents` | `traceAI-livekit` | Automatic |
| `pipecat` | `pipecat-ai` | `traceAI-pipecat` | Automatic (spans need `enable_tracing=True` on the `PipelineTask`) |
| `elevenlabs_agents` | `elevenlabs` | `traceAI-elevenlabs-agents` | Runs on ElevenLabs: forward webhooks with `traceai-elevenlabs-agents serve` |
| `exa` | `exa-py` | `traceAI-exa` | Automatic |
| `firecrawl` | `firecrawl-py` | `traceAI-firecrawl` | Automatic |
| `tavily` | `tavily-python` | `traceAI-tavily` | Automatic |
| `parallel` | `parallel-web` | `traceAI-parallel` | Automatic |
| `chromadb` | `chromadb` | `traceAI-chromadb` | Automatic |
| `pinecone` | `pinecone` | `traceAI-pinecone` | Automatic |
| `qdrant` | `qdrant-client` | `traceAI-qdrant` | Automatic |
| `weaviate` | `weaviate-client` | `traceAI-weaviate` | Automatic |
| `milvus` | `pymilvus` | `traceAI-milvus` | Automatic |
| `lancedb` | `lancedb` | `traceAI-lancedb` | Automatic |
| `mongodb` | `pymongo` | `traceAI-mongodb` | Automatic |
| `pgvector` | `pgvector` | `traceAI-pgvector` | Automatic |
| `redis` | `redisvl` or `redis` search | `traceAI-redis` | Automatic |

Web servers make each incoming request a root span, so the AI calls it causes are nested under it. They use the OpenTelemetry instrumentation packages: `fastapi`, `flask`, `django`, `falcon` and `tornado` turn on automatically when `opentelemetry-instrumentation-<name>` is installed, and `fi-instrument bootstrap --install --all` installs the ones your app needs.

</details>

## Configuration

Set these before the app starts. Arguments to `init()` come first, then the environment, then `futureagi.toml`, then the defaults.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Unset | Both are needed to export to Future AGI. Without them the app runs untouched and one warning is printed. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, else the working directory's name | Project in Observe. Set it for stable naming. |
| `FI_PROJECT_TYPE` | `observe` | `observe` or `experiment`. |
| `FI_PROJECT_VERSION_NAME` | Unset | Version name, for `experiment` projects. |
| `FI_BASE_URL` | `https://api.futureagi.com` | Collector base URL. |
| `FI_INSTRUMENTATIONS` | Every installed integration | Comma-separated allowlist of integration names, for example `openai,langchain`. |
| `FI_DISABLED_INSTRUMENTATIONS` | None | Comma-separated denylist. It wins over the allowlist. |
| `FI_SAMPLE_RATE` | `1.0` | Fraction of traces to keep, decided per trace. |
| `FI_AUTO_DEBUG` | `false` | Print the integration report at startup. |
| `FI_CONFIG_FILE` | `./futureagi.toml` | Config file to read. A named file must exist. |
| `FI_AUTO_INSTRUMENT` | Unset | `true` starts the agent in every Python process of the environment. |
| `OTEL_SDK_DISABLED` | `false` | `true` turns tracing off. |

The keys are read as **one pair from one source**: a key from the environment is never combined with a secret from the config file. A source that sets only one of the two is ignored with a warning that names the missing one.

### futureagi.toml

`fi-instrument init-config` writes a commented template in the current directory. The file is meant to be committed, so keep the keys in the environment. Every key matches an environment variable without its `FI_` prefix (`debug` is `FI_AUTO_DEBUG`):

```toml
project_name = "checkout-agent"
project_type = "observe"
disabled_instrumentations = ["bedrock"]
sample_rate = 0.25
hide_inputs = true
```

The format is a flat subset of TOML: one `key = value` per line, with quoted strings, `true`/`false`, numbers or one-line arrays of strings. `[tables]` are ignored.

## Privacy

Prompts, responses and tool content are **captured by default**, exactly as with a manual traceAI setup. The usual settings hide them, as environment variables or as `futureagi.toml` keys:

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

In code, pass `config=TraceConfig(...)` to `init()`. `fi-instrument doctor` shows the value in effect for each setting and where it came from.

## Existing OpenTelemetry setup

- **You call `register()` yourself.** The agent reuses that provider and adds nothing.
- **Your app configures its own OpenTelemetry provider.** Call `fi_instrumentation.init()` after that setup, or pass `tracer_provider=`. The agent keeps your sampler, processors and exporters, and adds one Future AGI export next to them. Spans sent to Future AGI carry the project name; your other exporters see the spans unchanged. `FI_SAMPLE_RATE` then thins only the Future AGI copy.
- **`fi-instrument` and `FI_AUTO_INSTRUMENT` start before your code**, so the agent creates and registers the provider. OpenTelemetry allows only one global provider, so an app that sets its own should use `init()` as above instead.

## Short-lived processes and shutdown

Spans are exported in batches and flushed when the process exits. On `SIGTERM` the agent flushes for up to five seconds, then runs the handler that was there before (or the default action), so the exit code is unchanged. It does not touch `SIGINT`, and a handler your app installs later still takes over as usual.

For a job or serverless handler that must export before it returns, flush explicitly:

```python
from fi_instrumentation import auto

auto.flush()      # export buffered spans now
auto.shutdown()   # stop the agent; it cannot be restarted in the same process
```

## Commands

| Command | What it does |
| --- | --- |
| `fi-instrument python app.py` | Run a program with tracing (also `fi-instrument run -- ...`). |
| `fi-instrument doctor` | Show the settings and their sources, every installed integration's status, and whether the collector accepts the keys. `--json` for machine-readable output, `--offline` to skip the collector check, `--all` to list libraries that are not installed. |
| `fi-instrument bootstrap` | List the traceAI packages missing for installed libraries. `--install` installs them; `--all` also covers vector databases and web servers. |
| `fi-instrument init-config` | Write a `futureagi.toml` template. |
| `fi-instrument validate` | Check the settings and send one empty export; exit 1 on a problem. `--offline` checks the settings only. |

`python -m fi_instrumentation.auto` runs the same commands. `doctor` runs as a separate process: `pending` there means the library is installed, not that your running app has imported it. Set `FI_AUTO_DEBUG=true` to print the report from inside the app.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `FI_API_KEY and FI_SECRET_KEY are not set; tracing is off` | Set both keys in the same place (environment, `init()` arguments or `futureagi.toml`). |
| A library shows `missing_instrumentor` | Run `fi-instrument bootstrap --install`, then restart the app. |
| A library shows `manual` | Add the setup call shown in the detail. |
| A library stays `pending` | The app has not imported it yet, or it was imported in another process. |
| A library shows `failed` | The detail has the error, often an unsupported library version. The app keeps running. |
| Nothing arrives in Future AGI | Run `fi-instrument validate` to check `FI_BASE_URL` and the keys, and let the app exit normally or call `auto.flush()`. |
| Duplicate spans | Remove manual `instrument()` calls for the same library, or disable that integration with `FI_DISABLED_INSTRUMENTATIONS`. |

## Limits

- Only libraries with an installed traceAI package are traced. Calls made with a plain HTTP client, or through a library without a traceAI package, are not traced in this release.
- Each traceAI package keeps its own supported library versions and Python versions.
- The agent loads at interpreter start, which adds the cost of importing the core package and OpenTelemetry to each traced process.
- `FI_AUTO_INSTRUMENT` needs the installed wheel; with `pip install -e` or `poetry install`, use `fi-instrument`.
