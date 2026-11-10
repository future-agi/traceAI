// Package traceaieino traces CloudWeGo Eino applications
// (github.com/cloudwego/eino) for Future AGI.
//
// [Handler] is an Eino callbacks.Handler, the framework's own extension
// point. Register it once with callbacks.AppendGlobalHandlers before any graph
// or agent runs (or pass it to one run with compose.WithCallbacks or
// adk.WithCallbacks), and every component run Eino reports becomes one
// OpenTelemetry span: graphs, chains and workflows, their nodes, chat models,
// tools, retrievers, embedders, prompts, lambdas and ADK agents. A span's
// parent is the span of the enclosing run, taken from the context Eino passes
// to the callback, so one graph or agent run is one trace.
//
// Spans carry the attribute names Python traceAI uses: gen_ai.span.kind (LLM,
// TOOL, AGENT, CHAIN, RETRIEVER, EMBEDDING, UNKNOWN), gen_ai.request.model,
// token usage, messages, tool calls, input.value and output.value. Model name
// and token counts are written only when the component reported them in its
// callback payload; a streamed model output is one span whose usage comes from
// the chunk that reports it, never a sum of chunks.
//
// Content (prompts, replies, tool arguments and results, retrieved text) is
// captured by default, as in every traceAI instrumentor. The traceAI
// switches turn it off: FI_HIDE_INPUTS, FI_HIDE_OUTPUTS,
// FI_HIDE_INPUT_MESSAGES, FI_HIDE_OUTPUT_MESSAGES, FI_HIDE_INPUT_TEXT,
// FI_HIDE_OUTPUT_TEXT, FI_HIDE_INPUT_IMAGES,
// FI_HIDE_LLM_INVOCATION_PARAMETERS, or a [TraceConfig] in code. Errors are
// recorded as error.type plus a scrubbed one-line error.message (no stack
// trace, URL, path, host or email), and no message at all when inputs or
// outputs are hidden.
//
// The package depends only on the OpenTelemetry API and Eino. It does not
// create an exporter: build a TracerProvider with the stock OTLP/HTTP exporter
// pointed at Future AGI (see the example directory) and pass it with
// [WithTracerProvider], or install it as the global provider.
package traceaieino
