package traceailangchaingo

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/tmc/langchaingo/llms"
	"go.opentelemetry.io/otel/codes"
)

func TestPrivacyEnvironmentAndCredentials(t *testing.T) {
	t.Setenv("FI_HIDE_INPUT_TEXT", "TRUE")
	t.Setenv("FI_HIDE_OUTPUT_TEXT", "True")
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleLLMGenerateContentStart(ctx, []llms.MessageContent{{Parts: []llms.ContentPart{llms.TextPart("private input")}}})
	h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "private output"}}})
	h.HandleToolStart(ctx, "authorization_token=synthetic-demo-token")
	h.HandleToolEnd(ctx, "api_key=synthetic-secret")
	for _, span := range exp.GetSpans() {
		if strings.Contains(attr(span.Attributes, "input.value"), "private") || strings.Contains(attr(span.Attributes, "output.value"), "private") || strings.Contains(attr(span.Attributes, "input.value"), "synthetic-") || strings.Contains(attr(span.Attributes, "output.value"), "synthetic-") {
			t.Fatalf("sensitive content recorded: %v", span.Attributes)
		}
	}
}
func TestHideInputsUppercase(t *testing.T) {
	t.Setenv("FI_HIDE_INPUTS", "TRUE")
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleToolStart(ctx, "private")
	h.HandleToolEnd(ctx, "ok")
	if attr(exp.GetSpans()[0].Attributes, "input.value") != "" {
		t.Fatal("uppercase hide ignored")
	}
}
func TestCredentialRedactionWithoutPrivacySwitch(t *testing.T) {
	t.Setenv("FI_HIDE_INPUTS", "false")
	t.Setenv("FI_HIDE_INPUT_TEXT", "false")
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleToolStart(ctx, "authorization_token=synthetic-demo-token payload=hello")
	h.HandleToolEnd(ctx, "ok")
	value := attr(exp.GetSpans()[0].Attributes, "input.value")
	if strings.Contains(value, "synthetic-demo-token") || !strings.Contains(value, "payload=hello") {
		t.Fatalf("tool input not safely captured: %q", value)
	}
}
func TestEmbedderUnderActiveChain(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleChainStart(ctx, nil)
	_, err := h.WrapEmbedder(fakeEmbed{}).EmbedQuery(ctx, "query")
	if err != nil {
		t.Fatal(err)
	}
	h.HandleChainEnd(ctx, nil)
	spans := exp.GetSpans()
	if len(spans) != 2 || spans[0].Parent.SpanID() != spans[1].SpanContext.SpanID() {
		t.Fatal("orphan embedding")
	}
}
func TestMistralNilEndThenErrorAndSuccess(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleLLMGenerateContentEnd(ctx, nil)
	h.HandleLLMError(ctx, errors.New("failed"))
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleLLMGenerateContentEnd(ctx, nil)
	h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "ok"}}})
	spans := exp.GetSpans()
	if len(spans) != 2 || spans[0].Status.Code != codes.Error || attr(spans[1].Attributes, "output.value") != "ok" {
		t.Fatalf("incorrect Mistral callbacks: %+v", spans)
	}
}
func TestModelFromResponseOrOption(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{GenerationInfo: map[string]any{"Model": "model-response"}}}})
	if attr(exp.GetSpans()[0].Attributes, "gen_ai.request.model") != "model-response" {
		t.Fatal("response model omitted")
	}
	WithModel("model-config")(h)
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "ok"}}})
	if attr(exp.GetSpans()[1].Attributes, "gen_ai.request.model") != "model-config" {
		t.Fatal("configured model omitted")
	}
}
func TestOversizedStreamRetainsPrefix(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleStreamingFunc(ctx, []byte("start"))
	h.HandleStreamingFunc(ctx, []byte(strings.Repeat("x", 5000)))
	h.HandleStreamingFunc(ctx, []byte("end"))
	h.HandleLLMGenerateContentEnd(ctx, nil)
	h.EndRun(ctx)
	value := attr(exp.GetSpans()[0].Attributes, "output.value")
	if len(value) != 4096 || !strings.HasPrefix(value, "startxxx") {
		t.Fatalf("stream prefix lost: len=%d prefix=%q", len(value), value[:min(len(value), 8)])
	}
}

func TestJSONCredentialAndModelInput(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleToolStart(ctx, `{"authorization_token":"synthetic-json-token","query":"hello"}`)
	h.HandleToolEnd(ctx, "ok")
	if strings.Contains(attr(exp.GetSpans()[0].Attributes, "input.value"), "synthetic-json-token") {
		t.Fatal("JSON credential leaked")
	}
	WithModel("configured")(h)
	h.HandleLLMGenerateContentStart(ctx, []llms.MessageContent{{Parts: []llms.ContentPart{llms.BinaryPart("image/png", []byte("synthetic"))}}, {Parts: []llms.ContentPart{llms.TextPart("second message")}}})
	h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "ok"}}})
	if attr(exp.GetSpans()[1].Attributes, "input.value") != "second message" {
		t.Fatal("model option skipped input text")
	}
}

func TestScopeVersion(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleToolStart(ctx, "x")
	h.HandleToolEnd(ctx, "y")
	if got := exp.GetSpans()[0].InstrumentationScope.Version; got != Version {
		t.Fatalf("scope version=%q", got)
	}
}

func TestHiddenStreamNilEndIsSuccessful(t *testing.T) {
	h, exp := setup(t)
	h.hideOutputs = true
	ctx := ContextWithRun(context.Background())
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleStreamingFunc(ctx, []byte("private"))
	h.HandleLLMGenerateContentEnd(ctx, nil)
	h.EndRun(ctx)
	spans := exp.GetSpans()
	if len(spans) != 1 || spans[0].Status.Code == codes.Error || attr(spans[0].Attributes, "output.value") != "" {
		t.Fatalf("hidden stream incorrectly finalized: %+v", spans)
	}
}
