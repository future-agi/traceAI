package traceailangchaingo

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/tmc/langchaingo/llms"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func setup(t *testing.T) (*Handler, *tracetest.InMemoryExporter) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return NewHandler(WithTracerProvider(tp)), exp
}
func attr(attrs []attribute.KeyValue, key string) string {
	for _, a := range attrs {
		if string(a.Key) == key {
			return a.Value.Emit()
		}
	}
	return ""
}
func TestNestedAndErrorCleanup(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleChainStart(ctx, nil)
	h.HandleLLMGenerateContentStart(ctx, []llms.MessageContent{{Parts: []llms.ContentPart{llms.TextPart("secret")}}})
	h.HandleStreamingFunc(ctx, []byte("partial"))
	h.HandleLLMError(ctx, errors.New("secret error"))
	h.HandleToolStart(ctx, "input")
	h.HandleToolEnd(ctx, "output")
	h.HandleChainEnd(ctx, nil)
	spans := exp.GetSpans()
	if len(spans) != 3 {
		t.Fatalf("got %d spans", len(spans))
	}
	if spans[0].Status.Code != codes.Error {
		t.Fatal("error span expected")
	}
	if spans[0].Parent.SpanID() != spans[2].SpanContext.SpanID() || spans[1].Parent.SpanID() != spans[2].SpanContext.SpanID() {
		t.Fatal("lost parent")
	}
	if attr(spans[0].Attributes, "output.value") != "" {
		t.Fatal("failed stream exposed partial content")
	}
	if attr(spans[0].Attributes, "exception.message") != "" {
		t.Fatal("error text leaked")
	}
}
func TestConcurrentRunsAndPrivacy(t *testing.T) {
	h, exp := setup(t)
	h.hideInputs = true
	h.hideOutputs = true
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx := ContextWithRun(context.Background())
			h.HandleChainStart(ctx, nil)
			h.HandleLLMGenerateContentStart(ctx, []llms.MessageContent{{Parts: []llms.ContentPart{llms.TextPart("private")}}})
			h.HandleStreamingFunc(ctx, []byte("private"))
			h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "private", GenerationInfo: map[string]any{"PromptTokens": 3, "CompletionTokens": 2}}}})
			h.HandleChainEnd(ctx, nil)
		}()
	}
	wg.Wait()
	spans := exp.GetSpans()
	if len(spans) != 64 {
		t.Fatalf("got %d spans", len(spans))
	}
	for _, s := range spans {
		if attr(s.Attributes, "input.value") != "" || attr(s.Attributes, "output.value") != "" {
			t.Fatal("private content leaked")
		}
		if attr(s.Attributes, "langchaingo.operation") == "llm" && attr(s.Attributes, "gen_ai.usage.input_tokens") != "3" {
			t.Fatal("missing usage")
		}
	}
}

type fakeEmbed struct{}

func (fakeEmbed) EmbedQuery(ctx context.Context, text string) ([]float32, error) {
	return []float32{1, 2}, nil
}
func (fakeEmbed) EmbedDocuments(ctx context.Context, texts []string) ([][]float32, error) {
	return [][]float32{{1, 2}}, nil
}
func TestEmbedder(t *testing.T) {
	h, exp := setup(t)
	e := h.WrapEmbedder(fakeEmbed{})
	_, err := e.EmbedQuery(context.Background(), "secret")
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.EmbedDocuments(context.Background(), []string{"secret"})
	if err != nil {
		t.Fatal(err)
	}
	if len(exp.GetSpans()) != 2 {
		t.Fatal("embed spans missing")
	}
	for _, s := range exp.GetSpans() {
		if attr(s.Attributes, "input.value") != "" {
			t.Fatal("embedded text leaked")
		}
	}
}

func TestEndRunClosesUnmatchedRetriever(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleRetrieverStart(ctx, "secret")
	h.EndRun(ctx)
	h.EndRun(ctx)
	spans := exp.GetSpans()
	if len(spans) != 1 || spans[0].Status.Code != codes.Error {
		t.Fatalf("unexpected orphan spans: %d", len(spans))
	}
}
func TestStreamCompletionFallback(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleStreamingFunc(ctx, []byte("hello"))
	h.HandleLLMGenerateContentEnd(ctx, nil)
	spans := exp.GetSpans()
	if len(spans) != 1 || attr(spans[0].Attributes, "output.value") != "hello" {
		t.Fatal("missing successful streamed output")
	}
}

type fakeModel struct{ callbacks *Handler }

func (m fakeModel) GenerateContent(ctx context.Context, messages []llms.MessageContent, _ ...llms.CallOption) (*llms.ContentResponse, error) {
	m.callbacks.HandleLLMGenerateContentStart(ctx, messages)
	res := &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "Kyoto has Kinkaku-ji", GenerationInfo: map[string]any{"PromptTokens": 6, "CompletionTokens": 7}}}}
	m.callbacks.HandleLLMGenerateContentEnd(ctx, res)
	return res, nil
}
func (m fakeModel) Call(ctx context.Context, prompt string, options ...llms.CallOption) (string, error) {
	res, err := m.GenerateContent(ctx, []llms.MessageContent{{Parts: []llms.ContentPart{llms.TextPart(prompt)}}}, options...)
	if err != nil {
		return "", err
	}
	return res.Choices[0].Content, nil
}
func TestLangChainModelCall(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	answer, err := llms.GenerateFromSinglePrompt(ctx, fakeModel{h}, "Kyoto?")
	h.EndRun(ctx)
	if err != nil || answer != "Kyoto has Kinkaku-ji" {
		t.Fatalf("answer=%q err=%v", answer, err)
	}
	spans := exp.GetSpans()
	if len(spans) != 1 || attr(spans[0].Attributes, "gen_ai.span.kind") != "LLM" || attr(spans[0].Attributes, "gen_ai.usage.output_tokens") != "7" {
		t.Fatalf("bad model span: %+v", spans)
	}
}
