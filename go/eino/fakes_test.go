package traceaieino

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/components"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/components/tool/utils"
	"github.com/cloudwego/eino/schema"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// recorder returns an in-memory span recorder and a handler that exports to
// it. The handler's privacy switches are tc.
func recorder(t *testing.T, tc TraceConfig) (*tracetest.SpanRecorder, *sdktrace.TracerProvider, *Handler) {
	t.Helper()
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return rec, tp, NewHandler(WithTracerProvider(tp), WithTraceConfig(tc))
}

// waitEnded flushes background span ends (streams, agent events).
func waitEnded(t *testing.T, h *Handler) {
	t.Helper()
	h.pending.Wait()
}

func attrMap(s sdktrace.ReadOnlySpan) map[string]attribute.Value {
	m := map[string]attribute.Value{}
	for _, kv := range s.Attributes() {
		m[string(kv.Key)] = kv.Value
	}
	return m
}

func attr(s sdktrace.ReadOnlySpan, key string) (string, bool) {
	v, ok := attrMap(s)[key]
	if !ok {
		return "", false
	}
	return v.String(), true
}

func byName(spans []sdktrace.ReadOnlySpan, name string) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range spans {
		if s.Name() == name {
			out = append(out, s)
		}
	}
	return out
}

func one(t *testing.T, spans []sdktrace.ReadOnlySpan, name string) sdktrace.ReadOnlySpan {
	t.Helper()
	got := byName(spans, name)
	if len(got) != 1 {
		t.Fatalf("want one span %q, got %d; spans: %s", name, len(got), tree(spans))
	}
	return got[0]
}

// tree renders spans as an indented parent/child list for failure messages.
func tree(spans []sdktrace.ReadOnlySpan) string {
	children := map[trace.SpanID][]sdktrace.ReadOnlySpan{}
	ids := map[trace.SpanID]bool{}
	for _, s := range spans {
		ids[s.SpanContext().SpanID()] = true
	}
	var roots []sdktrace.ReadOnlySpan
	for _, s := range spans {
		p := s.Parent().SpanID()
		if !s.Parent().IsValid() || !ids[p] {
			roots = append(roots, s)
			continue
		}
		children[p] = append(children[p], s)
	}
	var b strings.Builder
	var walk func(s sdktrace.ReadOnlySpan, depth int)
	walk = func(s sdktrace.ReadOnlySpan, depth int) {
		kind, _ := attr(s, GenAISpanKind)
		fmt.Fprintf(&b, "\n%s- %s [%s] %s", strings.Repeat("  ", depth), s.Name(), kind, s.Status().Code)
		kids := children[s.SpanContext().SpanID()]
		sort.Slice(kids, func(i, j int) bool { return kids[i].StartTime().Before(kids[j].StartTime()) })
		for _, c := range kids {
			walk(c, depth+1)
		}
	}
	for _, r := range roots {
		walk(r, 0)
	}
	return b.String()
}

// fakeModel is a ToolCallingChatModel that answers from a script. The first
// call asks for the weather tool (when tools are bound and the conversation
// has no tool result yet); later calls answer in text. With callbacks set it
// fires its own callbacks with typed payloads (model name, usage), the way
// eino-ext models do; without, the graph fires them with the raw messages.
type fakeModel struct {
	name      string
	callbacks bool
	usage     bool
	fail      error
	tools     []*schema.ToolInfo
	answer    string
	mu        sync.Mutex
	calls     int
}

var (
	_ model.ToolCallingChatModel = (*fakeModel)(nil)
	_ components.Checker         = (*fakeModel)(nil)
	_ components.Typer           = (*fakeModel)(nil)
)

func (m *fakeModel) GetType() string          { return "Fake" }
func (m *fakeModel) IsCallbacksEnabled() bool { return m.callbacks }

func (m *fakeModel) WithTools(tools []*schema.ToolInfo) (model.ToolCallingChatModel, error) {
	return &fakeModel{name: m.name, callbacks: m.callbacks, usage: m.usage, fail: m.fail, tools: tools, answer: m.answer}, nil
}

func (m *fakeModel) toolsFor(opts []model.Option) []*schema.ToolInfo {
	if o := model.GetCommonOptions(nil, opts...); o != nil && len(o.Tools) > 0 {
		return o.Tools
	}
	return m.tools
}

func (m *fakeModel) reply(in []*schema.Message, tools []*schema.ToolInfo) *schema.Message {
	m.mu.Lock()
	m.calls++
	m.mu.Unlock()
	hasToolResult := false
	for _, msg := range in {
		if msg.Role == schema.Tool {
			hasToolResult = true
		}
	}
	out := &schema.Message{Role: schema.Assistant, ResponseMeta: &schema.ResponseMeta{FinishReason: "stop"}}
	if len(tools) > 0 && !hasToolResult {
		out.ToolCalls = []schema.ToolCall{{ID: "call_1", Type: "function",
			Function: schema.FunctionCall{Name: tools[0].Name, Arguments: `{"city":"Paris"}`}}}
		out.ResponseMeta.FinishReason = "tool_calls"
	} else {
		out.Content = m.answer
		if out.Content == "" {
			out.Content = "It is sunny in Paris."
		}
	}
	if m.usage {
		out.ResponseMeta.Usage = &schema.TokenUsage{PromptTokens: 11, CompletionTokens: 7, TotalTokens: 18}
	}
	return out
}

func (m *fakeModel) config() *model.Config {
	return &model.Config{Model: m.name, MaxTokens: 256, Temperature: 0.2}
}

func (m *fakeModel) Generate(ctx context.Context, in []*schema.Message, opts ...model.Option) (*schema.Message, error) {
	tools := m.toolsFor(opts)
	if m.callbacks {
		ctx = callbacks.EnsureRunInfo(ctx, m.GetType(), components.ComponentOfChatModel)
		ctx = callbacks.OnStart(ctx, &model.CallbackInput{Messages: in, Tools: tools, Config: m.config()})
	}
	if m.fail != nil {
		if m.callbacks {
			callbacks.OnError(ctx, m.fail)
		}
		return nil, m.fail
	}
	out := m.reply(in, tools)
	if m.callbacks {
		var u *model.TokenUsage
		if out.ResponseMeta.Usage != nil {
			u = &model.TokenUsage{PromptTokens: 11, CompletionTokens: 7, TotalTokens: 18}
		}
		callbacks.OnEnd(ctx, &model.CallbackOutput{Message: out, Config: m.config(), TokenUsage: u})
	}
	return out, nil
}

// Stream sends the reply in three chunks; usage (when on) only in the last.
func (m *fakeModel) Stream(ctx context.Context, in []*schema.Message, opts ...model.Option) (*schema.StreamReader[*schema.Message], error) {
	tools := m.toolsFor(opts)
	if m.callbacks {
		ctx = callbacks.EnsureRunInfo(ctx, m.GetType(), components.ComponentOfChatModel)
		ctx = callbacks.OnStart(ctx, &model.CallbackInput{Messages: in, Tools: tools, Config: m.config()})
	}
	if m.fail != nil {
		if m.callbacks {
			callbacks.OnError(ctx, m.fail)
		}
		return nil, m.fail
	}
	full := m.reply(in, tools)
	var chunks []*schema.Message
	if len(full.ToolCalls) > 0 {
		chunks = []*schema.Message{{Role: schema.Assistant, ToolCalls: full.ToolCalls}}
	} else {
		words := strings.SplitAfter(full.Content, " ")
		for _, w := range words {
			chunks = append(chunks, &schema.Message{Role: schema.Assistant, Content: w})
		}
	}
	last := &schema.Message{Role: schema.Assistant, ResponseMeta: &schema.ResponseMeta{FinishReason: full.ResponseMeta.FinishReason}}
	if m.usage {
		last.ResponseMeta.Usage = &schema.TokenUsage{PromptTokens: 11, CompletionTokens: 7, TotalTokens: 18}
	}
	chunks = append(chunks, last)
	sr, sw := schema.Pipe[*schema.Message](len(chunks))
	go func() {
		defer sw.Close()
		for _, c := range chunks {
			sw.Send(c, nil)
		}
	}()
	if !m.callbacks {
		return sr, nil
	}
	cbStream := schema.StreamReaderWithConvert(sr, func(msg *schema.Message) (callbacks.CallbackOutput, error) {
		var u *model.TokenUsage
		if msg.ResponseMeta != nil && msg.ResponseMeta.Usage != nil {
			u = &model.TokenUsage{PromptTokens: 11, CompletionTokens: 7, TotalTokens: 18}
		}
		return &model.CallbackOutput{Message: msg, Config: m.config(), TokenUsage: u}, nil
	})
	_, out := callbacks.OnEndWithStreamOutput(ctx, cbStream)
	return schema.StreamReaderWithConvert(out, func(o callbacks.CallbackOutput) (*schema.Message, error) {
		return o.(*model.CallbackOutput).Message, nil
	}), nil
}

type weatherIn struct {
	City string `json:"city" jsonschema:"description=the city"`
}

// weatherTool returns a tool that reports the weather, or fails with err.
func weatherTool(t *testing.T, err error) tool.InvokableTool {
	t.Helper()
	tl, e := utils.InferTool("get_weather", "Get the weather for a city",
		func(ctx context.Context, in *weatherIn) (string, error) {
			if err != nil {
				return "", err
			}
			return `{"city":"` + in.City + `","sky":"sunny"}`, nil
		})
	if e != nil {
		t.Fatal(e)
	}
	return tl
}

// collect reads a stream to the end.
func collect[T any](t *testing.T, sr *schema.StreamReader[T]) []T {
	t.Helper()
	defer sr.Close()
	var out []T
	for {
		v, err := sr.Recv()
		if errors.Is(err, io.EOF) {
			return out
		}
		if err != nil {
			t.Fatalf("stream: %v", err)
		}
		out = append(out, v)
	}
}
