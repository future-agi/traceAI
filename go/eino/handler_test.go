package traceaieino

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/cloudwego/eino/adk"
	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/components"
	"github.com/cloudwego/eino/components/embedding"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/components/prompt"
	"github.com/cloudwego/eino/components/retriever"
	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/compose"
	"github.com/cloudwego/eino/schema"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// weatherGraph is START -> model -> tools -> END, named weather_graph.
func weatherGraph(t *testing.T, m *fakeModel, toolErr error) compose.Runnable[[]*schema.Message, []*schema.Message] {
	t.Helper()
	ctx := context.Background()
	wt := weatherTool(t, toolErr)
	info, err := wt.Info(ctx)
	if err != nil {
		t.Fatal(err)
	}
	bound, err := m.WithTools([]*schema.ToolInfo{info})
	if err != nil {
		t.Fatal(err)
	}
	tn, err := compose.NewToolNode(ctx, &compose.ToolsNodeConfig{Tools: []tool.BaseTool{wt}})
	if err != nil {
		t.Fatal(err)
	}
	g := compose.NewGraph[[]*schema.Message, []*schema.Message]()
	must(t, g.AddChatModelNode("model", bound, compose.WithNodeName("chat")))
	must(t, g.AddToolsNode("tools", tn))
	must(t, g.AddEdge(compose.START, "model"))
	must(t, g.AddEdge("model", "tools"))
	must(t, g.AddEdge("tools", compose.END))
	r, err := g.Compile(ctx, compose.WithGraphName("weather_graph"))
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// AC-03: one graph run is one trace; every node is a child, no orphans.
func TestGraphRunIsOneTraceWithNodeChildren(t *testing.T) {
	for _, cb := range []bool{false, true} {
		t.Run(fmt.Sprintf("component_callbacks=%v", cb), func(t *testing.T) {
			rec, _, h := recorder(t, TraceConfig{})
			r := weatherGraph(t, &fakeModel{name: "fake-model-1", callbacks: cb, usage: true}, nil)
			ctx := ContextWithSessionID(context.Background(), "sess-1")
			ctx = ContextWithUserID(ctx, "user-1")
			if _, err := r.Invoke(ctx, []*schema.Message{schema.UserMessage("weather in Paris?")}, compose.WithCallbacks(h)); err != nil {
				t.Fatal(err)
			}
			waitEnded(t, h)
			spans := rec.Ended()
			if len(spans) != 4 {
				t.Fatalf("want graph, model, tools node and tool spans, got:%s", tree(spans))
			}
			root := one(t, spans, "weather_graph")
			chat := one(t, spans, "chat")
			tools := one(t, spans, "ToolsNode")
			tl := one(t, spans, "get_weather")
			if root.Parent().IsValid() {
				t.Fatalf("graph span must be the root")
			}
			tid := root.SpanContext().TraceID()
			for _, s := range spans {
				if s.SpanContext().TraceID() != tid {
					t.Fatalf("span %s is in another trace", s.Name())
				}
				if got, _ := attr(s, SessionID); got != "sess-1" {
					t.Errorf("%s: session.id = %q", s.Name(), got)
				}
				if got, _ := attr(s, UserID); got != "user-1" {
					t.Errorf("%s: user.id = %q", s.Name(), got)
				}
				if s.Status().Code != codes.Ok {
					t.Errorf("%s: status %v", s.Name(), s.Status())
				}
			}
			if chat.Parent().SpanID() != root.SpanContext().SpanID() || tools.Parent().SpanID() != root.SpanContext().SpanID() {
				t.Fatalf("nodes must be children of the graph:%s", tree(spans))
			}
			if tl.Parent().SpanID() != tools.SpanContext().SpanID() {
				t.Fatalf("tool must be a child of the tools node:%s", tree(spans))
			}
			wantKinds := map[string]string{"weather_graph": SpanKindChain, "chat": SpanKindLLM, "ToolsNode": SpanKindChain, "get_weather": SpanKindTool}
			for name, kind := range wantKinds {
				if got, _ := attr(one(t, spans, name), GenAISpanKind); got != kind {
					t.Errorf("%s kind = %s, want %s", name, got, kind)
				}
			}
			if got, _ := attr(chat, GenAIOperationName); got != "chat" {
				t.Errorf("chat op = %q", got)
			}
			if got, _ := attr(tl, GenAIToolName); got != "get_weather" {
				t.Errorf("tool name = %q", got)
			}
			if got, _ := attr(tl, GenAIToolCallID); got != "call_1" {
				t.Errorf("tool call id = %q", got)
			}
			if got, _ := attr(tl, GenAIToolCallArguments); got != `{"city":"Paris"}` {
				t.Errorf("tool args = %q", got)
			}
			if got, _ := attr(tl, GenAIToolCallResult); !strings.Contains(got, "sunny") {
				t.Errorf("tool result = %q", got)
			}
			if got, _ := attr(chat, GenAIOutputMessages+".0."+MessageToolCalls+".0."+ToolCallFunctionName); got != "get_weather" {
				t.Errorf("model tool call = %q", got)
			}
			if got, _ := attr(chat, GenAIInputMessages+".0."+MessageContent); got != "weather in Paris?" {
				t.Errorf("input message = %q", got)
			}
			if got, _ := attr(chat, GenAIUsageInputTokens); got != "11" {
				t.Errorf("input tokens = %q", got)
			}
			if got, _ := attr(chat, GenAIUsageTotalTokens); got != "18" {
				t.Errorf("total tokens = %q", got)
			}
			// The model name is only known when the component reports it.
			model, ok := attr(chat, GenAIRequestModel)
			if cb && model != "fake-model-1" {
				t.Errorf("model = %q", model)
			}
			if !cb && ok {
				t.Errorf("model %q invented for a component that did not report it", model)
			}
			if cb {
				if got, _ := attr(chat, GenAIToolDefinitions+".0."+ToolJSONSchema); !strings.Contains(got, `"get_weather"`) {
					t.Errorf("tool definition = %q", got)
				}
			}
		})
	}
}

// AC-04: a streamed model node is one span; usage comes from the chunk that
// reports it and is not summed; the reply text is joined.
func TestStreamingModelIsOneSpanWithFinalUsage(t *testing.T) {
	for _, cb := range []bool{false, true} {
		t.Run(fmt.Sprintf("component_callbacks=%v", cb), func(t *testing.T) {
			rec, _, h := recorder(t, TraceConfig{})
			m := &fakeModel{name: "fake-model-1", callbacks: cb, usage: true, answer: "It is sunny in Paris today."}
			ch := compose.NewChain[[]*schema.Message, *schema.Message]()
			ch.AppendChatModel(m, compose.WithNodeName("chat"))
			r, err := ch.Compile(context.Background(), compose.WithGraphName("stream_chain"))
			must(t, err)
			sr, err := r.Stream(context.Background(), []*schema.Message{schema.UserMessage("hi")}, compose.WithCallbacks(h))
			must(t, err)
			chunks := collect(t, sr)
			if len(chunks) < 3 {
				t.Fatalf("want a real stream, got %d chunks", len(chunks))
			}
			waitEnded(t, h)
			spans := rec.Ended()
			chat := one(t, spans, "chat")
			for k, want := range map[string]string{
				GenAIUsageInputTokens: "11", GenAIUsageOutputTokens: "7", GenAIUsageTotalTokens: "18",
				OutputValue: "It is sunny in Paris today.",
			} {
				if got, _ := attr(chat, k); got != want {
					t.Errorf("%s = %q, want %q", k, got, want)
				}
			}
			root := one(t, spans, "stream_chain")
			if chat.Parent().SpanID() != root.SpanContext().SpanID() {
				t.Fatalf("chat must be a child of the chain:%s", tree(spans))
			}
			if !chat.EndTime().After(chat.StartTime()) && !chat.EndTime().Equal(chat.StartTime()) {
				t.Fatalf("bad span times")
			}
		})
	}
}

// AC-05: OnError then OnEnd for the same run exports one ERROR span.
func TestErrorThenEndExportsOneErrorSpan(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "flaky", Type: "Fake", Component: components.ComponentOfChatModel}
	ctx := h.OnStart(context.Background(), info, &model.CallbackInput{Messages: []*schema.Message{schema.UserMessage("x")}})
	h.OnError(ctx, info, errors.New("upstream said no"))
	h.OnEnd(ctx, info, &model.CallbackOutput{Message: schema.AssistantMessage("late", nil)})
	h.OnError(ctx, info, errors.New("again"))
	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want one span, got %d", len(spans))
	}
	s := spans[0]
	if s.Status().Code != codes.Error {
		t.Fatalf("status = %v", s.Status())
	}
	if got, _ := attr(s, OutputValue); got != "" {
		t.Fatalf("a late OnEnd must not add output: %q", got)
	}
	if got, _ := attr(s, ErrorType); got != "error" {
		t.Fatalf("error.type = %q", got)
	}
}

// OnEnd and OnError racing on one run end it exactly once.
func TestConcurrentEndAndErrorEndOnce(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	for i := 0; i < 50; i++ {
		info := &callbacks.RunInfo{Name: "race", Component: compose.ComponentOfLambda}
		ctx := h.OnStart(context.Background(), info, "in")
		var wg sync.WaitGroup
		wg.Add(2)
		go func() { defer wg.Done(); h.OnEnd(ctx, info, "out") }()
		go func() { defer wg.Done(); h.OnError(ctx, info, errors.New("boom")) }()
		wg.Wait()
	}
	if n := len(rec.Ended()); n != 50 {
		t.Fatalf("want 50 spans, got %d", n)
	}
}

// A failing node marks the node and the graph ERROR; the tool error is on the
// tool span.
func TestFailingToolMarksToolNodeAndGraphError(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	r := weatherGraph(t, &fakeModel{name: "fake-model-1", callbacks: true, usage: true}, errors.New("weather service down"))
	_, err := r.Invoke(context.Background(), []*schema.Message{schema.UserMessage("weather?")}, compose.WithCallbacks(h))
	if err == nil {
		t.Fatal("want an error")
	}
	waitEnded(t, h)
	spans := rec.Ended()
	for _, name := range []string{"weather_graph", "ToolsNode", "get_weather"} {
		if s := one(t, spans, name); s.Status().Code != codes.Error {
			t.Errorf("%s status = %v", name, s.Status())
		}
	}
	if s := one(t, spans, "chat"); s.Status().Code != codes.Ok {
		t.Errorf("chat status = %v", s.Status())
	}
	tl := one(t, spans, "get_weather")
	if got, _ := attr(tl, ErrorMessage); !strings.Contains(got, "weather service down") {
		t.Errorf("error.message = %q", got)
	}
	if len(tl.Events()) != 0 {
		t.Errorf("no exception event is recorded, got %d events", len(tl.Events()))
	}
}

// A model that fails before it reports OnStart still gets one ERROR span, and
// the enclosing graph span is not ended early by that error.
func TestErrorWithoutStartGetsItsOwnSpan(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	graph := &callbacks.RunInfo{Name: "g", Component: compose.ComponentOfGraph}
	ctx := h.OnStart(context.Background(), graph, "in")
	failed := &callbacks.RunInfo{Type: "Fake", Component: components.ComponentOfChatModel}
	h.OnError(ctx, failed, errors.New("bad request"))
	if n := len(rec.Ended()); n != 1 {
		t.Fatalf("want only the failed model span ended, got %d", n)
	}
	h.OnEnd(ctx, graph, "out")
	spans := rec.Ended()
	g, m := one(t, spans, "g"), one(t, spans, "FakeChatModel")
	if g.Status().Code != codes.Ok || m.Status().Code != codes.Error {
		t.Fatalf("graph %v model %v", g.Status(), m.Status())
	}
	if m.Parent().SpanID() != g.SpanContext().SpanID() {
		t.Fatalf("failed model must be a child of the graph")
	}
}

// AC-06: a component Eino does not know is kept, not dropped or relabelled.
func TestUnknownComponentIsKept(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "my_reranker", Type: "Custom", Component: components.Component("Reranker")}
	ctx := h.OnStart(context.Background(), info, map[string]any{"query": "q"})
	h.OnEnd(ctx, info, []string{"a", "b"})
	s := one(t, rec.Ended(), "my_reranker")
	if got, _ := attr(s, GenAISpanKind); got != SpanKindUnknown {
		t.Fatalf("kind = %q", got)
	}
	if got, _ := attr(s, EinoComponent); got != "Reranker" {
		t.Fatalf("eino.component = %q", got)
	}
	if got, _ := attr(s, OutputValue); got != `["a","b"]` {
		t.Fatalf("output.value = %q", got)
	}
}

// AC-07: no usage attribute is written when the payload has none.
func TestUsageOmittedWhenAbsent(t *testing.T) {
	for _, stream := range []bool{false, true} {
		rec, _, h := recorder(t, TraceConfig{})
		m := &fakeModel{name: "fake-model-1", callbacks: true, usage: false}
		ch := compose.NewChain[[]*schema.Message, *schema.Message]()
		ch.AppendChatModel(m, compose.WithNodeName("chat"))
		r, err := ch.Compile(context.Background())
		must(t, err)
		in := []*schema.Message{schema.UserMessage("hi")}
		if stream {
			sr, err := r.Stream(context.Background(), in, compose.WithCallbacks(h))
			must(t, err)
			collect(t, sr)
		} else {
			_, err = r.Invoke(context.Background(), in, compose.WithCallbacks(h))
			must(t, err)
		}
		waitEnded(t, h)
		chat := one(t, rec.Ended(), "chat")
		for k := range attrMap(chat) {
			if strings.HasPrefix(k, "gen_ai.usage.") {
				t.Errorf("stream=%v: %s written without usage in the payload", stream, k)
			}
		}
	}
}

// Usage is the largest reported total, never a sum of chunks.
func TestStreamUsageIsNeverSummed(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "chat", Component: components.ComponentOfChatModel}
	ctx := h.OnStart(context.Background(), info, &model.CallbackInput{Messages: []*schema.Message{schema.UserMessage("x")}})
	sr, sw := schema.Pipe[callbacks.CallbackOutput](4)
	// A provider that repeats a running total in every chunk.
	for i, u := range []int{5, 9, 12} {
		sw.Send(&model.CallbackOutput{
			Message:    &schema.Message{Role: schema.Assistant, Content: fmt.Sprint(i)},
			TokenUsage: &model.TokenUsage{PromptTokens: 4, CompletionTokens: u - 4, TotalTokens: u},
		}, nil)
	}
	sw.Close()
	h.OnEndWithStreamOutput(ctx, info, sr)
	waitEnded(t, h)
	s := one(t, rec.Ended(), "chat")
	if got, _ := attr(s, GenAIUsageTotalTokens); got != "12" {
		t.Fatalf("total = %s, want 12 (the final running total)", got)
	}
	if got, _ := attr(s, OutputValue); got != "012" {
		t.Fatalf("output = %q", got)
	}
}

// A stream that fails mid-way ends its span ERROR.
func TestStreamErrorEndsSpanWithError(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "chat", Component: components.ComponentOfChatModel}
	ctx := h.OnStart(context.Background(), info, &model.CallbackInput{Messages: []*schema.Message{schema.UserMessage("x")}})
	sr, sw := schema.Pipe[callbacks.CallbackOutput](2)
	sw.Send(&model.CallbackOutput{Message: schema.AssistantMessage("par", nil)}, nil)
	sw.Send(nil, context.DeadlineExceeded)
	sw.Close()
	h.OnEndWithStreamOutput(ctx, info, sr)
	waitEnded(t, h)
	s := one(t, rec.Ended(), "chat")
	if s.Status().Code != codes.Error {
		t.Fatalf("status = %v", s.Status())
	}
	if got, _ := attr(s, ErrorType); got != "timeout" {
		t.Fatalf("error.type = %q", got)
	}
}

// AC-08: content is captured by default and dropped by the hide switches,
// everywhere: attributes, events and status.
func TestContentDefaultOnAndHiddenBySwitches(t *testing.T) {
	const marker = "MARKER-7d1f"
	run := func(tc TraceConfig) []sdktrace.ReadOnlySpan {
		rec, _, h := recorder(t, tc)
		m := &fakeModel{name: "fake-model-1", callbacks: true, usage: true, answer: "Sunny. " + marker}
		r := weatherGraph(t, m, nil)
		_, err := r.Invoke(context.Background(), []*schema.Message{schema.UserMessage("weather? " + marker)}, compose.WithCallbacks(h))
		must(t, err)
		waitEnded(t, h)
		return rec.Ended()
	}
	contains := func(spans []sdktrace.ReadOnlySpan, s string) []string {
		var hits []string
		for _, sp := range spans {
			for _, kv := range sp.Attributes() {
				if strings.Contains(kv.Value.String(), s) {
					hits = append(hits, sp.Name()+":"+string(kv.Key))
				}
			}
			for _, ev := range sp.Events() {
				if strings.Contains(fmt.Sprint(ev), s) {
					hits = append(hits, sp.Name()+":event")
				}
			}
			if strings.Contains(sp.Status().Description, s) || strings.Contains(sp.Name(), s) {
				hits = append(hits, sp.Name()+":status/name")
			}
		}
		return hits
	}
	if hits := contains(run(TraceConfig{}), marker); len(hits) == 0 {
		t.Fatal("default must capture content")
	}
	hidden := run(TraceConfig{HideInputs: true, HideOutputs: true})
	if hits := contains(hidden, marker); len(hits) != 0 {
		t.Fatalf("hidden run leaked content: %v", hits)
	}
	// Tool arguments and results are content too.
	if hits := contains(hidden, "Paris"); len(hits) != 0 {
		t.Fatalf("hidden run leaked tool arguments or results: %v", hits)
	}
	chat := one(t, hidden, "chat")
	if got, _ := attr(chat, InputValue); got != RedactedValue {
		t.Fatalf("input.value = %q", got)
	}
	if got, _ := attr(chat, GenAIUsageTotalTokens); got != "18" {
		t.Fatalf("usage must stay when content is hidden: %q", got)
	}
	// Messages only.
	msgsOnly := run(TraceConfig{HideInputMessages: true, HideOutputMessages: true})
	for k := range attrMap(one(t, msgsOnly, "chat")) {
		if strings.HasPrefix(k, GenAIInputMessages) || strings.HasPrefix(k, GenAIOutputMessages) {
			t.Fatalf("%s written with message capture off", k)
		}
	}
}

// The FI_HIDE_* variables are read when the handler is created.
func TestHideSwitchesFromEnvironment(t *testing.T) {
	clearHideEnv(t)
	t.Setenv(EnvHideInputs, "true")
	t.Setenv(EnvHideOutputs, "TRUE")
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	h := NewHandler(WithTracerProvider(tp))
	info := &callbacks.RunInfo{Name: "t", Component: components.ComponentOfTool}
	ctx := h.OnStart(context.Background(), info, &tool.CallbackInput{ArgumentsInJSON: `{"q":"SECRET"}`})
	h.OnEnd(ctx, info, &tool.CallbackOutput{Response: "SECRET"})
	for _, kv := range rec.Ended()[0].Attributes() {
		if strings.Contains(kv.Value.String(), "SECRET") {
			t.Fatalf("%s leaked", kv.Key)
		}
	}
}

// Error messages are scrubbed by default and dropped when content is hidden.
func TestErrorMessageScrubbedAndHidden(t *testing.T) {
	err := fmt.Errorf("[NodeRunError] call failed: Post \"http://10.1.2.3:8317/v1/chat\": dial tcp 10.1.2.3:8317: refused; see /Users/alice/app/main.go:42 or mail ops@example.com\ngoroutine 1 [running]:\nmain.main()\n\t/Users/alice/app/main.go:42")
	check := func(tc TraceConfig) (string, bool, sdktrace.ReadOnlySpan) {
		rec, _, h := recorder(t, tc)
		info := &callbacks.RunInfo{Name: "n", Component: compose.ComponentOfLambda}
		ctx := h.OnStart(context.Background(), info, "x")
		h.OnError(ctx, info, err)
		s := rec.Ended()[0]
		msg, ok := attr(s, ErrorMessage)
		return msg, ok, s
	}
	msg, ok, s := check(TraceConfig{})
	if !ok || !strings.Contains(msg, "call failed") {
		t.Fatalf("error.message = %q", msg)
	}
	for _, leak := range []string{"10.1.2.3", "/Users/alice", "ops@example.com", "goroutine", "http://", "8317"} {
		if strings.Contains(msg, leak) || strings.Contains(s.Status().Description, leak) {
			t.Errorf("leaked %q in %q / %q", leak, msg, s.Status().Description)
		}
	}
	if _, ok, _ := check(TraceConfig{HideInputs: true}); ok {
		t.Fatal("error.message must be dropped when inputs are hidden")
	}
	if _, ok, _ := check(TraceConfig{HideOutputs: true}); ok {
		t.Fatal("error.message must be dropped when outputs are hidden")
	}
}

func TestErrorType(t *testing.T) {
	type apiErr struct{ error }
	cases := map[string]error{
		"canceled":            fmt.Errorf("x: %w", context.Canceled),
		"timeout":             context.DeadlineExceeded,
		"error":               errors.New("plain"),
		"*traceaieino.apiErr": fmt.Errorf("wrapped: %w", &apiErr{errors.New("x")}),
	}
	for want, err := range cases {
		if got := errorType(err); got != want && !(want == "*traceaieino.apiErr" && strings.HasSuffix(got, "apiErr")) {
			t.Errorf("errorType(%v) = %q, want %q", err, got, want)
		}
	}
}

// AC-10: after Shutdown a callback does not panic and exports nothing; a run
// started before Shutdown still ends.
func TestShutdownThenCallbacksAreNoOps(t *testing.T) {
	rec, tp, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "before", Component: compose.ComponentOfLambda}
	ctx := h.OnStart(context.Background(), info, "x")
	must(t, h.Shutdown(context.Background()))
	h.OnEnd(ctx, info, "y")
	if n := len(rec.Ended()); n != 1 {
		t.Fatalf("the run started before Shutdown must still end, got %d spans", n)
	}
	late := &callbacks.RunInfo{Name: "late", Component: components.ComponentOfChatModel}
	ctx2 := h.OnStart(context.Background(), late, &model.CallbackInput{})
	h.OnEnd(ctx2, late, &model.CallbackOutput{})
	h.OnError(ctx2, late, errors.New("x"))
	in, inW := schema.Pipe[callbacks.CallbackInput](1)
	inW.Close()
	h.OnStartWithStreamInput(context.Background(), late, in)
	out, outW := schema.Pipe[callbacks.CallbackOutput](1)
	outW.Close()
	h.OnEndWithStreamOutput(ctx2, late, out)
	must(t, h.Shutdown(context.Background()))
	// And after the provider itself is shut down.
	must(t, tp.Shutdown(context.Background()))
	h2 := NewHandler(WithTracerProvider(tp))
	c := h2.OnStart(context.Background(), late, nil)
	h2.OnEnd(c, late, nil)
	if n := len(rec.Ended()); n != 1 {
		t.Fatalf("late callbacks exported %d spans", n-1)
	}
}

// AC-12: a callback with no parent context starts a new trace; it never
// attaches to, or ends, another run's span.
func TestNoParentContextStartsANewTrace(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	r := weatherGraph(t, &fakeModel{name: "m", callbacks: true}, nil)
	_, err := r.Invoke(context.Background(), []*schema.Message{schema.UserMessage("a")}, compose.WithCallbacks(h))
	must(t, err)
	first := one(t, rec.Ended(), "weather_graph").SpanContext().TraceID()

	info := &callbacks.RunInfo{Name: "standalone", Component: components.ComponentOfTool}
	ctx := h.OnStart(context.Background(), info, &tool.CallbackInput{ArgumentsInJSON: "{}"})
	h.OnEnd(ctx, info, &tool.CallbackOutput{Response: "ok"})
	s := one(t, rec.Ended(), "standalone")
	if s.Parent().IsValid() || s.SpanContext().TraceID() == first {
		t.Fatal("a run without a parent context must be a new root")
	}

	// OnEnd with a context that carries another run's state does nothing.
	outer := &callbacks.RunInfo{Name: "outer", Component: compose.ComponentOfGraph}
	octx := h.OnStart(context.Background(), outer, "x")
	h.OnEnd(octx, &callbacks.RunInfo{Name: "stranger", Component: compose.ComponentOfLambda}, "y")
	if len(byName(rec.Ended(), "outer")) != 0 {
		t.Fatal("an unrelated OnEnd ended the outer span")
	}
	h.OnEnd(octx, outer, "x")
}

// A nil RunInfo (a component that never set one) does not panic.
func TestNilRunInfo(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	ctx := h.OnStart(context.Background(), nil, "x")
	h.OnEnd(ctx, nil, "y")
	s := one(t, rec.Ended(), "eino.run")
	if got, _ := attr(s, GenAISpanKind); got != SpanKindUnknown {
		t.Fatalf("kind = %q", got)
	}
}

// V-D: registering the handler twice gives two spans per component, nested,
// and every one of them ends.
func TestDoubleRegistrationGivesTwoSpansPerComponent(t *testing.T) {
	t.Run("per-run", func(t *testing.T) {
		rec, _, h := recorder(t, TraceConfig{})
		r := weatherGraph(t, &fakeModel{name: "m", callbacks: true}, nil)
		_, err := r.Invoke(context.Background(), []*schema.Message{schema.UserMessage("a")}, compose.WithCallbacks(h, h))
		must(t, err)
		waitEnded(t, h)
		for _, name := range []string{"weather_graph", "chat", "ToolsNode", "get_weather"} {
			if n := len(byName(rec.Ended(), name)); n != 2 {
				t.Errorf("%s: %d spans, want 2:%s", name, n, tree(rec.Ended()))
			}
		}
	})
	t.Run("global", func(t *testing.T) {
		rec, _, h := recorder(t, TraceConfig{})
		callbacks.AppendGlobalHandlers(h, h)
		t.Cleanup(func() { callbacks.InitCallbackHandlers(nil) })
		r := weatherGraph(t, &fakeModel{name: "m", callbacks: true}, nil)
		_, err := r.Invoke(context.Background(), []*schema.Message{schema.UserMessage("a")})
		must(t, err)
		waitEnded(t, h)
		if n := len(byName(rec.Ended(), "chat")); n != 2 {
			t.Fatalf("chat: %d spans, want 2", n)
		}
	})
}

// Registered once with AppendGlobalHandlers, the handler sees every run.
func TestGlobalRegistrationTracesEveryRun(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	callbacks.AppendGlobalHandlers(h)
	t.Cleanup(func() { callbacks.InitCallbackHandlers(nil) })
	r := weatherGraph(t, &fakeModel{name: "m", callbacks: true}, nil)
	for i := 0; i < 2; i++ {
		_, err := r.Invoke(context.Background(), []*schema.Message{schema.UserMessage("a")})
		must(t, err)
	}
	if n := len(byName(rec.Ended(), "weather_graph")); n != 2 {
		t.Fatalf("want 2 graph spans, got %d", n)
	}
}

// otherHandler stands in for another vendor's callback handler (Langfuse,
// APMPlus, ...) on its own tracer provider: it puts its own span in the
// context it returns.
type otherHandler struct{ tracer trace.Tracer }

func (o otherHandler) OnStart(ctx context.Context, info *callbacks.RunInfo, _ callbacks.CallbackInput) context.Context {
	ctx, _ = o.tracer.Start(ctx, "other")
	return ctx
}
func (o otherHandler) OnEnd(ctx context.Context, _ *callbacks.RunInfo, _ callbacks.CallbackOutput) context.Context {
	trace.SpanFromContext(ctx).End()
	return ctx
}
func (o otherHandler) OnError(ctx context.Context, _ *callbacks.RunInfo, _ error) context.Context {
	trace.SpanFromContext(ctx).End()
	return ctx
}
func (o otherHandler) OnStartWithStreamInput(ctx context.Context, _ *callbacks.RunInfo, in *schema.StreamReader[callbacks.CallbackInput]) context.Context {
	in.Close()
	return ctx
}
func (o otherHandler) OnEndWithStreamOutput(ctx context.Context, _ *callbacks.RunInfo, out *schema.StreamReader[callbacks.CallbackOutput]) context.Context {
	out.Close()
	return ctx
}

// Another handler's span is never made the parent of ours.
func TestAnotherHandlersSpanIsNotTheParent(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	otherTP := sdktrace.NewTracerProvider()
	other := otherHandler{tracer: otherTP.Tracer("other")}
	r := weatherGraph(t, &fakeModel{name: "m", callbacks: true}, nil)
	_, err := r.Invoke(context.Background(), []*schema.Message{schema.UserMessage("a")}, compose.WithCallbacks(other, h))
	must(t, err)
	spans := rec.Ended()
	ids := map[trace.SpanID]bool{}
	for _, s := range spans {
		ids[s.SpanContext().SpanID()] = true
	}
	for _, s := range spans {
		if s.Parent().IsValid() && !ids[s.Parent().SpanID()] {
			t.Errorf("%s has a parent outside this provider:%s", s.Name(), tree(spans))
		}
	}
	if n := len(spans); n != 4 {
		t.Fatalf("want 4 spans, got:%s", tree(spans))
	}
}

// A span the caller starts before the run, or inside a node, is the parent.
func TestCallerSpanIsTheParent(t *testing.T) {
	rec, tp, h := recorder(t, TraceConfig{})
	ctx, root := tp.Tracer("app").Start(context.Background(), "request")
	var inner trace.SpanContext
	lam := compose.InvokableLambda(func(ctx context.Context, in string) (string, error) {
		// A sub-chain run from inside a lambda, under a span the lambda starts.
		cctx, span := tp.Tracer("app").Start(ctx, "lambda-work")
		inner = span.SpanContext()
		sub := compose.NewChain[string, string]()
		sub.AppendLambda(compose.InvokableLambda(func(ctx context.Context, s string) (string, error) { return s + "!", nil }), compose.WithNodeName("sub_step"))
		r, err := sub.Compile(cctx)
		if err != nil {
			return "", err
		}
		// No WithCallbacks here: the run inherits the handler from ctx.
		out, err := r.Invoke(cctx, in)
		span.End()
		return out, err
	})
	ch := compose.NewChain[string, string]()
	ch.AppendLambda(lam, compose.WithNodeName("outer_step"))
	r, err := ch.Compile(ctx, compose.WithGraphName("outer"))
	must(t, err)
	_, err = r.Invoke(ctx, "hi", compose.WithCallbacks(h))
	must(t, err)
	root.End()
	spans := rec.Ended()
	if p := one(t, spans, "outer").Parent(); p.SpanID() != root.SpanContext().SpanID() {
		t.Fatalf("graph must be a child of the caller's span:%s", tree(spans))
	}
	if n := len(byName(spans, "sub_step")); n != 1 {
		t.Fatalf("an inherited handler must give one span per node, got %d:%s", n, tree(spans))
	}
	var sub sdktrace.ReadOnlySpan
	for _, s := range spans {
		if s.Name() == "sub_step" {
			sub = s
		}
	}
	if sub == nil {
		t.Fatalf("no sub_step span:%s", tree(spans))
	}
	// sub_step's parent is the sub-chain's graph span, whose parent is lambda-work.
	var subChain sdktrace.ReadOnlySpan
	for _, s := range spans {
		if s.SpanContext().SpanID() == sub.Parent().SpanID() {
			subChain = s
		}
	}
	if subChain == nil || subChain.Parent().SpanID() != inner.SpanID() {
		t.Fatalf("a run started inside a caller span must nest under it:%s", tree(spans))
	}
}

// An ADK agent run is one trace rooted at the agent span, which ends after
// the agent's last event; the model and tool runs are inside it.
func TestADKAgentRun(t *testing.T) {
	for _, streaming := range []bool{false, true} {
		t.Run(fmt.Sprintf("streaming=%v", streaming), func(t *testing.T) {
			rec, _, h := recorder(t, TraceConfig{})
			ctx := context.Background()
			ag, err := adk.NewChatModelAgent(ctx, &adk.ChatModelAgentConfig{
				Name: "weather_agent", Description: "answers weather questions", Instruction: "be brief",
				Model:       &fakeModel{name: "fake-model-1", callbacks: true, usage: true, answer: "Sunny in Paris."},
				ToolsConfig: adk.ToolsConfig{ToolsNodeConfig: compose.ToolsNodeConfig{Tools: []tool.BaseTool{weatherTool(t, nil)}}},
			})
			must(t, err)
			runner := adk.NewRunner(ctx, adk.RunnerConfig{Agent: ag, EnableStreaming: streaming})
			it := runner.Query(ContextWithSessionID(ctx, "agent-sess"), "weather in Paris?", adk.WithCallbacks(h))
			for {
				ev, ok := it.Next()
				if !ok {
					break
				}
				if ev.Err != nil {
					t.Fatal(ev.Err)
				}
				if mo := ev.Output; mo != nil && mo.MessageOutput != nil && mo.MessageOutput.IsStreaming {
					collect(t, mo.MessageOutput.MessageStream)
				}
			}
			waitEnded(t, h)
			spans := rec.Ended()
			var agent sdktrace.ReadOnlySpan
			for _, s := range spans {
				if k, _ := attr(s, GenAISpanKind); k == SpanKindAgent {
					agent = s
				}
			}
			if agent == nil || agent.Parent().IsValid() {
				t.Fatalf("want a root agent span:%s", tree(spans))
			}
			if got, _ := attr(agent, GenAIAgentName); got != "weather_agent" {
				t.Errorf("agent name = %q", got)
			}
			if got, _ := attr(agent, OutputValue); got != "Sunny in Paris." {
				t.Errorf("agent output = %q", got)
			}
			if got, _ := attr(agent, InputValue); !strings.Contains(got, "weather in Paris?") {
				t.Errorf("agent input = %q", got)
			}
			tid := agent.SpanContext().TraceID()
			var llm, tl int
			for _, s := range spans {
				if s.SpanContext().TraceID() != tid {
					t.Fatalf("%s outside the agent trace", s.Name())
				}
				if s.StartTime().Before(agent.StartTime()) {
					t.Errorf("%s starts before the agent span", s.Name())
				}
				// Every run ends with the agent. A streamed run's span ends
				// when this handler has read its own copy of the stream,
				// which runs alongside the agent's event stream, so with
				// streaming it can end a moment after the agent span.
				if s.EndTime().After(agent.EndTime().Add(50 * time.Millisecond)) {
					t.Errorf("%s ends %v after the agent span", s.Name(), s.EndTime().Sub(agent.EndTime()))
				}
				if got, _ := attr(s, SessionID); got != "agent-sess" {
					t.Errorf("%s session.id = %q", s.Name(), got)
				}
				switch k, _ := attr(s, GenAISpanKind); k {
				case SpanKindLLM:
					llm++
				case SpanKindTool:
					tl++
				}
			}
			if llm != 2 || tl != 1 {
				t.Fatalf("want 2 model calls and 1 tool call, got %d/%d:%s", llm, tl, tree(spans))
			}
		})
	}
}

// An agent whose model fails ends ERROR.
func TestADKAgentErrorEndsAgentSpanWithError(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	ctx := context.Background()
	ag, err := adk.NewChatModelAgent(ctx, &adk.ChatModelAgentConfig{
		Name: "broken_agent", Description: "fails", Instruction: "x",
		Model: &fakeModel{name: "m", callbacks: true, fail: errors.New("model unavailable")},
	})
	must(t, err)
	it := adk.NewRunner(ctx, adk.RunnerConfig{Agent: ag}).Query(ctx, "hi", adk.WithCallbacks(h))
	sawErr := false
	for {
		ev, ok := it.Next()
		if !ok {
			break
		}
		if ev.Err != nil {
			sawErr = true
		}
	}
	if !sawErr {
		t.Fatal("want an error event")
	}
	waitEnded(t, h)
	var a sdktrace.ReadOnlySpan
	for _, s := range byName(rec.Ended(), "broken_agent") {
		if k, _ := attr(s, GenAISpanKind); k == SpanKindAgent {
			a = s
		}
	}
	if a == nil {
		t.Fatalf("no agent span:%s", tree(rec.Ended()))
	}
	if a.Status().Code != codes.Error {
		t.Fatalf("agent status = %v:%s", a.Status(), tree(rec.Ended()))
	}
}

// Retriever, embedder and prompt runs get their own kinds and attributes; the
// embedding vectors are never written.
func TestRetrieverEmbeddingPromptMapping(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	ctx := context.Background()

	ri := &callbacks.RunInfo{Name: "docs", Type: "Fake", Component: components.ComponentOfRetriever}
	c := h.OnStart(ctx, ri, &retriever.CallbackInput{Query: "what is eino", TopK: 2})
	d := (&schema.Document{ID: "d1", Content: "Eino is a Go framework"}).WithScore(0.9)
	h.OnEnd(c, ri, &retriever.CallbackOutput{Docs: []*schema.Document{d, {ID: "d2", Content: "x"}}})

	ei := &callbacks.RunInfo{Type: "Fake", Component: components.ComponentOfEmbedding}
	c = h.OnStart(ctx, ei, &embedding.CallbackInput{Texts: []string{"a", "b"}, Config: &embedding.Config{Model: "emb-1"}})
	h.OnEnd(c, ei, &embedding.CallbackOutput{Embeddings: [][]float64{{0.123456, 2}, {3, 4}},
		TokenUsage: &embedding.TokenUsage{PromptTokens: 4, TotalTokens: 4}})

	pi := &callbacks.RunInfo{Name: "tmpl", Component: components.ComponentOfPrompt}
	c = h.OnStart(ctx, pi, &prompt.CallbackInput{Variables: map[string]any{"city": "Paris"}})
	h.OnEnd(c, pi, &prompt.CallbackOutput{Result: []*schema.Message{schema.UserMessage("weather in Paris?")}})

	spans := rec.Ended()
	r := one(t, spans, "docs")
	for k, want := range map[string]string{
		GenAISpanKind: SpanKindRetriever, GenAIRetrievalQuery: "what is eino", GenAIRetrievalTopK: "2",
		RetrievalDocuments + ".0." + DocumentID: "d1", RetrievalDocuments + ".0." + DocumentScore: "0.9",
		RetrievalDocuments + ".0." + DocumentContent: "Eino is a Go framework", EinoRetrieverDocumentCount: "2",
	} {
		if got, _ := attr(r, k); got != want {
			t.Errorf("retriever %s = %q, want %q", k, got, want)
		}
	}
	if _, ok := attr(r, RetrievalDocuments+".1."+DocumentScore); ok {
		t.Error("no score is invented for a document without one")
	}
	e := one(t, spans, "FakeEmbedding")
	for k, want := range map[string]string{
		GenAISpanKind: SpanKindEmbedding, GenAIRequestModel: "emb-1", GenAIEmbeddingsDimension: "2",
		EinoEmbeddingCount: "2", GenAIUsageInputTokens: "4",
	} {
		if got, _ := attr(e, k); got != want {
			t.Errorf("embedding %s = %q, want %q", k, got, want)
		}
	}
	for _, kv := range e.Attributes() {
		if strings.Contains(kv.Value.String(), "0.123456") {
			t.Fatalf("embedding vector written in %s", kv.Key)
		}
	}
	p := one(t, spans, "tmpl")
	if got, _ := attr(p, InputValue); got != `{"city":"Paris"}` {
		t.Errorf("prompt input = %q", got)
	}
	if got, _ := attr(p, OutputValue); !strings.Contains(got, "weather in Paris?") {
		t.Errorf("prompt output = %q", got)
	}

	// Hidden: retrieved text and the query go.
	rec2, _, h2 := recorder(t, TraceConfig{HideInputs: true, HideOutputs: true})
	c = h2.OnStart(ctx, ri, &retriever.CallbackInput{Query: "SECRET-q"})
	h2.OnEnd(c, ri, &retriever.CallbackOutput{Docs: []*schema.Document{{ID: "d1", Content: "SECRET-doc"}}})
	for _, kv := range rec2.Ended()[0].Attributes() {
		if strings.Contains(kv.Value.String(), "SECRET") {
			t.Errorf("hidden retriever leaked %s", kv.Key)
		}
	}
}

// A streamed input (Transform / Collect lambdas) starts one span; with
// capture on, the chunks become input.value.
func TestStreamInputLambda(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	ch := compose.NewChain[string, string]()
	ch.AppendLambda(compose.StreamableLambda(func(ctx context.Context, in string) (*schema.StreamReader[string], error) {
		return schema.StreamReaderFromArray([]string{in, "-", in}), nil
	}), compose.WithNodeName("split"))
	ch.AppendLambda(compose.CollectableLambda(func(ctx context.Context, in *schema.StreamReader[string]) (string, error) {
		var b strings.Builder
		for _, s := range collect(t, in) {
			b.WriteString(s)
		}
		return b.String(), nil
	}), compose.WithNodeName("join"))
	r, err := ch.Compile(context.Background(), compose.WithGraphName("pipe"))
	must(t, err)
	out, err := r.Invoke(context.Background(), "ab", compose.WithCallbacks(h))
	must(t, err)
	if out != "ab-ab" {
		t.Fatalf("out = %q", out)
	}
	waitEnded(t, h)
	spans := rec.Ended()
	if n := len(byName(spans, "join")); n != 1 {
		t.Fatalf("join spans = %d:%s", n, tree(spans))
	}
	if n := len(byName(spans, "split")); n != 1 {
		t.Fatalf("split spans = %d:%s", n, tree(spans))
	}
	if got, _ := attr(one(t, spans, "split"), OutputValue); got != `["ab","-","ab"]` {
		t.Errorf("split output = %q", got)
	}
}

// The handler starts no exporter and adds no resource attributes.
func TestNoResourceOrExporterSideEffects(t *testing.T) {
	rec, _, h := recorder(t, TraceConfig{})
	info := &callbacks.RunInfo{Name: "x", Component: compose.ComponentOfLambda}
	ctx := h.OnStart(context.Background(), info, "a")
	h.OnEnd(ctx, info, "b")
	s := rec.Ended()[0]
	for _, kv := range s.Resource().Attributes() {
		if strings.HasPrefix(string(kv.Key), "host.") || strings.HasPrefix(string(kv.Key), "process.") {
			t.Fatalf("resource attribute %s added", kv.Key)
		}
	}
	if s.InstrumentationScope().Name != ScopeName || s.InstrumentationScope().Version != Version {
		t.Fatalf("scope = %+v", s.InstrumentationScope())
	}
}
