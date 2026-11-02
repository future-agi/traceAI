// Package traceailangchaingo instruments LangChainGo callbacks without replacing the model provider.
package traceailangchaingo

import (
	"context"
	"os"
	"regexp"
	"strings"
	"sync"

	"github.com/tmc/langchaingo/callbacks"
	"github.com/tmc/langchaingo/llms"
	"github.com/tmc/langchaingo/schema"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

const (
	Version   = "0.1.0"
	ScopeName = "github.com/future-agi/traceAI/go/langchaingo"
)

// Redact credential-shaped fields before clipping: a secret can straddle the clip boundary.
var credentialField = regexp.MustCompile(`(?i)(\b(?:authorization_token|authorization|access_token|refresh_token|api_key|x-api-key|secret_key|private_key|password|client_secret|bearer_token)\b["']?\s*[=:]\s*["']?)[^\s,"'&}]+`)
var bearerValue = regexp.MustCompile(`(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+`)

func safeContent(s string) string {
	return clipped(bearerValue.ReplaceAllString(credentialField.ReplaceAllString(s, "${1}[REDACTED]"), "${1}[REDACTED]"))
}
func envTrue(name string) bool { return strings.EqualFold(strings.TrimSpace(os.Getenv(name)), "true") }

type stateKey struct{}
type frame struct {
	kind          string
	span          trace.Span
	stream        strings.Builder
	streamed      bool
	pendingNilEnd bool
}
type runState struct {
	mu     sync.Mutex
	frames []*frame
}

// ContextWithRun isolates the callback stack for one invocation. Pass the returned
// context into the top-level chain/agent and its child calls; never share it across
// parallel runs. LangChainGo's callback interface cannot return a child context.
func ContextWithRun(ctx context.Context) context.Context {
	return context.WithValue(ctx, stateKey{}, &runState{})
}

type Handler struct {
	callbacks.SimpleHandler
	tracer                  trace.Tracer
	hideInputs, hideOutputs bool
	model                   string
}
type Option func(*Handler)

func WithTracerProvider(p trace.TracerProvider) Option {
	return func(h *Handler) { h.tracer = p.Tracer(ScopeName, trace.WithInstrumentationVersion(Version)) }
}
func WithHideInputs(v bool) Option  { return func(h *Handler) { h.hideInputs = v } }
func WithHideOutputs(v bool) Option { return func(h *Handler) { h.hideOutputs = v } }

// WithModel supplies the configured model when LangChainGo omits it from callbacks.
func WithModel(model string) Option { return func(h *Handler) { h.model = model } }
func NewHandler(opts ...Option) *Handler {
	h := &Handler{tracer: otel.Tracer(ScopeName, trace.WithInstrumentationVersion(Version)), hideInputs: envTrue("FI_HIDE_INPUTS") || envTrue("FI_HIDE_INPUT_TEXT"), hideOutputs: envTrue("FI_HIDE_OUTPUTS") || envTrue("FI_HIDE_OUTPUT_TEXT")}
	for _, o := range opts {
		o(h)
	}
	return h
}

var _ callbacks.Handler = (*Handler)(nil)

// EndRun closes callbacks that have no matching end (for example, a failed
// retriever). Defer it immediately after ContextWithRun and before provider shutdown.
func (h *Handler) EndRun(ctx context.Context) {
	s, ok := ctx.Value(stateKey{}).(*runState)
	if !ok {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := len(s.frames) - 1; i >= 0; i-- {
		f := s.frames[i]
		if f.pendingNilEnd && f.streamed {
			if !h.hideOutputs {
				f.span.SetAttributes(attribute.String("output.value", safeContent(f.stream.String())))
			}
		} else {
			f.span.SetStatus(codes.Error, "callback did not finish")
		}
		f.span.End()
		s.frames[i] = nil
	}
	s.frames = nil
}

func (h *Handler) start(ctx context.Context, kind, name string, attrs ...attribute.KeyValue) {
	s, ok := ctx.Value(stateKey{}).(*runState)
	if !ok {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if n := len(s.frames); n > 0 && s.frames[n-1].pendingNilEnd {
		f := s.frames[n-1]
		if f.stream.Len() > 0 && !h.hideOutputs {
			f.span.SetAttributes(attribute.String("output.value", safeContent(f.stream.String())))
		}
		if !f.streamed {
			f.span.SetStatus(codes.Error, "missing LLM response")
		}
		f.span.End()
		s.frames[n-1] = nil
		s.frames = s.frames[:n-1]
	}
	if n := len(s.frames); n > 0 {
		ctx = trace.ContextWithSpan(ctx, s.frames[n-1].span)
	}
	_, span := h.tracer.Start(ctx, name, trace.WithAttributes(append([]attribute.KeyValue{attribute.String("langchaingo.operation", kind), attribute.String("gen_ai.span.kind", strings.ToUpper(kind))}, attrs...)...))
	s.frames = append(s.frames, &frame{kind: kind, span: span})
}
func (h *Handler) finish(ctx context.Context, kind string, err error, attrs ...attribute.KeyValue) {
	s, ok := ctx.Value(stateKey{}).(*runState)
	if !ok {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	n := len(s.frames)
	if n == 0 || s.frames[n-1].kind != kind {
		return
	}
	f := s.frames[n-1]
	s.frames[n-1] = nil
	s.frames = s.frames[:n-1]
	if len(attrs) > 0 {
		f.span.SetAttributes(attrs...)
	}
	if err != nil {
		f.span.SetStatus(codes.Error, "operation failed")

	}
	if kind == "llm" && err == nil && !h.hideOutputs && f.stream.Len() > 0 {
		hasOutput := false
		for _, a := range attrs {
			if a.Key == "output.value" && a.Value.AsString() != "" {
				hasOutput = true
			}
		}
		if !hasOutput {
			f.span.SetAttributes(attribute.String("output.value", safeContent(f.stream.String())))
		}
	}
	f.span.End()
}
func clipped(s string) string {
	if len(s) > 4096 {
		return s[:4096]
	}
	return s
}
func (h *Handler) HandleChainStart(ctx context.Context, _ map[string]any) {
	h.start(ctx, "chain", "langchaingo.chain")
}
func (h *Handler) HandleChainEnd(ctx context.Context, _ map[string]any) { h.finish(ctx, "chain", nil) }
func (h *Handler) HandleChainError(ctx context.Context, err error)      { h.finish(ctx, "chain", err) }
func (h *Handler) HandleToolStart(ctx context.Context, input string) {
	if h.hideInputs {
		h.start(ctx, "tool", "langchaingo.tool")
	} else {
		h.start(ctx, "tool", "langchaingo.tool", attribute.String("input.value", safeContent(input)))
	}
}
func (h *Handler) HandleToolEnd(ctx context.Context, output string) {
	if h.hideOutputs {
		h.finish(ctx, "tool", nil)
	} else {
		h.finish(ctx, "tool", nil, attribute.String("output.value", safeContent(output)))
	}
}
func (h *Handler) HandleToolError(ctx context.Context, err error) { h.finish(ctx, "tool", err) }
func (h *Handler) HandleRetrieverStart(ctx context.Context, query string) {
	if h.hideInputs {
		h.start(ctx, "retriever", "langchaingo.retriever")
	} else {
		h.start(ctx, "retriever", "langchaingo.retriever", attribute.String("input.value", safeContent(query)))
	}
}
func (h *Handler) HandleRetrieverEnd(ctx context.Context, _ string, docs []schema.Document) {
	h.finish(ctx, "retriever", nil, attribute.Int("retriever.documents", len(docs)))
}
func (h *Handler) HandleLLMGenerateContentStart(ctx context.Context, ms []llms.MessageContent) {
	attrs := []attribute.KeyValue{attribute.String("gen_ai.operation.name", "chat")}
	if h.model != "" {
		attrs = append(attrs, attribute.String("gen_ai.request.model", h.model))
	}
	if !h.hideInputs {
	messages:
		for _, m := range ms {
			for _, p := range m.Parts {
				if t, ok := p.(llms.TextContent); ok {
					attrs = append(attrs, attribute.String("input.value", safeContent(t.Text)))
					break messages
				}
			}
		}
	}
	h.start(ctx, "llm", "langchaingo.chat", attrs...)
}
func (h *Handler) HandleLLMGenerateContentEnd(ctx context.Context, res *llms.ContentResponse) {
	// Some providers (Mistral v0.1.15) signal a nil end BEFORE either the
	// response or an error. Leave the span open until the definitive callback.
	if res == nil {
		if s, ok := ctx.Value(stateKey{}).(*runState); ok {
			s.mu.Lock()
			if n := len(s.frames); n > 0 && s.frames[n-1].kind == "llm" {
				s.frames[n-1].pendingNilEnd = true
			}
			s.mu.Unlock()
		}
		return
	}
	var attrs []attribute.KeyValue
	if res != nil && len(res.Choices) > 0 && res.Choices[0] != nil {
		c := res.Choices[0]
		if !h.hideOutputs {
			attrs = append(attrs, attribute.String("output.value", safeContent(c.Content)))
		}
		if c.StopReason != "" {
			attrs = append(attrs, attribute.StringSlice("gen_ai.response.finish_reasons", []string{c.StopReason}))
		}
		if h.model == "" {
			for _, key := range []string{"Model", "model", "model_name", "model_id"} {
				if model, ok := c.GenerationInfo[key].(string); ok && model != "" {
					attrs = append(attrs, attribute.String("gen_ai.request.model", model))
					break
				}
			}
		}
		for _, item := range []struct{ key, dest string }{{"PromptTokens", "gen_ai.usage.input_tokens"}, {"CompletionTokens", "gen_ai.usage.output_tokens"}, {"TotalTokens", "gen_ai.usage.total_tokens"}} {
			if v, ok := c.GenerationInfo[item.key]; ok {
				switch x := v.(type) {
				case int:
					attrs = append(attrs, attribute.Int(item.dest, x))
				case int64:
					attrs = append(attrs, attribute.Int64(item.dest, x))
				case float64:
					attrs = append(attrs, attribute.Int64(item.dest, int64(x)))
				}
			}
		}
	}
	h.finish(ctx, "llm", nil, attrs...)
}
func (h *Handler) HandleLLMError(ctx context.Context, err error) { h.finish(ctx, "llm", err) }
func (h *Handler) HandleStreamingFunc(ctx context.Context, chunk []byte) {
	s, ok := ctx.Value(stateKey{}).(*runState)
	if !ok {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.frames) == 0 {
		return
	}
	f := s.frames[len(s.frames)-1]
	if f.kind != "llm" {
		return
	}
	f.streamed = true
	if h.hideOutputs {
		return
	}
	if remaining := 4096 - f.stream.Len(); remaining > 0 {
		if len(chunk) > remaining {
			chunk = chunk[:remaining]
		}
		f.stream.Write(chunk)
	}
	// Do not export partial output: a subsequent error may invalidate the stream.
}
