// Package traceaia2a traces A2A (Agent2Agent) protocol client calls made with
// the official Go SDK, github.com/a2aproject/a2a-go/v2, for Future AGI.
//
// [Interceptor] is an a2aclient.CallInterceptor, the SDK's own extension
// point, attached with a2aclient.WithCallInterceptors. Each SendMessage,
// SendStreamingMessage, GetTask, CancelTask and SubscribeToTask call becomes
// one OpenTelemetry client span with gen_ai.span.kind=AGENT and the A2A
// attribute names Python traceai-a2a uses (gen_ai.a2a.task.id,
// gen_ai.a2a.task.state, gen_ai.a2a.agent.url, ...), each written only when
// a2a-go reports that fact. A stream is one span: every event is a span event
// on it, and the span ends at the reply message or at a terminal or
// interrupted task state. The W3C traceparent is written into the request's
// service parameters (HTTP headers on the JSON-RPC and REST transports), so a
// remote agent that reads it continues the same trace.
//
// The span is not an LLM span: the model call, if any, happens inside the
// remote agent and is not observed here.
//
// Content (the text you send and the agent's reply, artifacts and status
// message) is captured by default, as in every traceAI instrumentor. The
// traceAI switches turn it off: FI_HIDE_INPUTS, FI_HIDE_OUTPUTS (and the
// other FI_HIDE_* names, which this package honours on spans your own code
// starts), or a [TraceConfig] in code.
//
// The package depends only on the OpenTelemetry API and a2a-go. It does not
// create an exporter: build a TracerProvider with the stock OTLP/HTTP exporter
// pointed at Future AGI (see the example directory) and pass it with
// [WithTracerProvider], or install it as the global provider.
//
// Server-side handlers (a2asrv) are not instrumented by this package.
package traceaia2a
