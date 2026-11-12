# traceai

Initialize Future AGI OpenTelemetry tracing with one call. The `traceai.init`
helper can also enable instrumentors that you have installed separately.

[Documentation](https://docs.futureagi.com/docs/tracing) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install from this repository

This helper is not published by Future AGI on PyPI. The `traceai` name on PyPI
belongs to another project; do **not** use `pip install traceai` for this helper.
From the root of a traceAI checkout, install the local package instead:

```sh
pip install ./python/traceai
```

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment for Future AGI export.
This example creates a manual span; it does not call an LLM.

## Quick start

```python
import traceai

provider = traceai.init(
    project_name="my-ai-app",
    project_type="observe",
)
tracer = provider.get_tracer(__name__)
with tracer.start_as_current_span("example"):
    pass  # Put your application work here.
provider.shutdown()
```

To opt in to OpenAI instrumentation, install `traceAI-openai` separately and pass
`auto_instrument=["openai"]` to `traceai.init` before constructing the OpenAI client.
The helper does not install instrumentors or supply provider credentials. See
[OpenAI setup](https://docs.futureagi.com/docs/integrations/traceai/openai) for a
provider-specific example.

## Attachments (images, files, audio)

Attachments are **off** by default; upgrading captures no new media. They need a
`fi-instrumentation-otel` release that includes `fi_instrumentation.attachments`.

| Setting | Behavior |
|---|---|
| `attachment_config=AttachmentConfig(enabled=True)` or `FI_ATTACHMENTS_ENABLED=true` | The provider creates one attachment manager |
| `FI_ATTACHMENTS_ENABLED=false` | Kill switch: wins over code and denies every capture, even for a manager that is already running |
| `attachment_manager=` | Borrow your own manager instead. `traceai` never shuts it down and only adds the hide and PII flags of `trace_config` to it |
| `trace_config=TraceConfig(...)` | Hide flags and PII redaction also turn media capture off; nothing can relax them |

`traceai` passes the same manager to every instrumentor it enables (as `attachments=`),
and `traceai.get_attachment_manager()` returns it. Use `with_attachment_capture_policy(policy)`
to add a `CapturePolicy` restriction for some calls, and `with_attachment_ownership()` to
mark calls whose media your own code captures, so instrumentors do not upload it again.
Both return an OpenTelemetry context to attach around those calls.

MCP and agent tool media (screenshots, images, audio, files) uses the same manager:

| Integration | How to enable it |
|---|---|
| `openai_agents`, `claude_agent_sdk`, `smolagents`, `firecrawl` | `auto_instrument=[...]` with the name |
| `mcp` | `session = traceai.wrap_mcp_client(session)` on your `mcp.ClientSession` (the `mcp` instrumentor only propagates context) |
| `mcp_use` | `MCPAgent(..., callbacks=[traceai.mcp_use_callback()])` (mcp-use has a callback, not an instrumentor) |

Both helpers use the provider and hide flags from `traceai.init`, and pass the shared
manager only while capture is on.

`traceai.report()` shows the attachment state and, per integration, `media`:
`provider_adapter` when the installed instrumentor version captures media, with its
`media_operations`; `session_binding` for voice integrations that capture only through an
explicit `bind_*` call; otherwise `unqualified`. It contains no endpoint or credential and
is not a playback claim. A row can be `provider_adapter` for:

| Kind | Packages |
|---|---|
| Providers | `openai`, `anthropic`, `groq`, `google_genai`, `vertexai`, `bedrock`, `litellm`, `fireworks`, `xai`, `mistralai`, `openrouter`, `portkey`, `ollama`, `vllm`, `minimax` |
| Frameworks | `langchain`, `llamaindex`, `agno`, `pydantic_ai` |
| MCP and agents | the six packages above |

An installed version without the adapter reports `unqualified`. `together`, `deepseek` and
`cerebras` capture no media.

Agno reads `pii_redaction` and `hide_error_details` only from `FI_PII_REDACTION` and
`FI_HIDE_ERROR_DETAILS`. When `trace_config` sets either one in code and the environment does
not, `traceai` still traces Agno but captures no Agno media, and its status is
`config_unsupported`. Agno span text then does not follow that flag; set the environment
variable to apply it.

## License

Apache-2.0.
