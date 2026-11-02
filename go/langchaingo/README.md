# LangChainGo tracing

Trace LangChainGo chains, tools, retrievers and model calls to Future AGI. This callback adapter uses LangChainGo's `callbacks.Handler` and standard OpenTelemetry; no provider SDK is replaced. Requires Go 1.26+ and LangChainGo v0.1.15+.

## Install

```sh
go get github.com/future-agi/traceAI/go/langchaingo@latest
```

Until the first `go/langchaingo/v0.1.0` tag is released, use `@dev` instead of `@latest`.

## Configure tracing

Set `FI_API_KEY`, `FI_SECRET_KEY`, `FI_PROJECT_NAME` and the credential for your chosen model provider. The [example](example/main.go) configures an OpenTelemetry OTLP/HTTP exporter with the full endpoint `https://api.futureagi.com/tracer/v1/traces`, headers `X-Api-Key` and `X-Secret-Key`, no compression, and the `project_name` and `project_type=observe` resource attributes.

```go
handler := traceailangchaingo.NewHandler(traceailangchaingo.WithTracerProvider(tp))
model, err := openai.New(openai.WithCallback(handler))
if err != nil { return err }
ctx := traceailangchaingo.ContextWithRun(context.Background())
defer handler.EndRun(ctx)
response, err := llms.GenerateFromSinglePrompt(ctx, model, "Name one landmark in Kyoto.")
```

Use the same returned context for each nested chain/model/tool call in one run. Create a **new** `ContextWithRun` context for every parallel invocation: LangChainGo callback methods cannot return child contexts or distinguish overlapping operations sharing one context. Pass the handler to each LangChainGo component that accepts callbacks. Do not also instrument the same model call with a provider-specific semantic instrumentor, or you will see duplicate LLM spans.

LangChainGo's legacy `HandleLLMStart` callback has no matching success callback; use providers that emit `HandleLLMGenerateContentStart` / `HandleLLMGenerateContentEnd`. `HandleAgentAction` and `HandleAgentFinish` do not expose a matching start/end pair, so the surrounding chain is the agent's parent span. Retriever callbacks have no error callback; a failed retriever's span is closed by `EndRun` if no end callback arrives.

For embeddings, which have no callbacks in LangChainGo, wrap the embedder once:

```go
embedder = handler.WrapEmbedder(embedder)
```

`FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true` prevent capture of input and output text. You can also use `WithHideInputs(true)` and `WithHideOutputs(true)`. Binary images, reasoning blocks, tool arguments, document bodies and raw error text are never exported. Input and output text fields are limited to 4 KiB. Streaming chunks are buffered to at most 4 KiB and used only when a successful completion has no response text. On error, partial output is discarded. Token counts use the provider's `PromptTokens`, `CompletionTokens` and `TotalTokens` fields when present; missing counts are not inferred.

To find a run, open the project named by `FI_PROJECT_NAME` in Future AGI and inspect its traces. If no spans appear, verify that the exact `ContextWithRun` context reached the callback, that your model was given the handler, and that the provider flushes on shutdown.
