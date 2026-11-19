package traceailangchaingo

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/tmc/langchaingo/llms"
	"go.opentelemetry.io/otel/codes"
)

func TestMissingLLMEndDoesNotFailSuccessfulRun(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleChainStart(ctx, nil)
	h.HandleLLMGenerateContentStart(ctx, nil) // Anthropic returns without an end callback.
	h.HandleChainEnd(ctx, nil)
	h.EndRun(ctx)
	spans := exp.GetSpans()
	if len(spans) != 2 || spans[0].Status.Code == codes.Error || spans[1].Status.Code == codes.Error || spans[0].Parent.SpanID() != spans[1].SpanContext.SpanID() {
		t.Fatalf("successful chain/LLM ended incorrectly: %+v", spans)
	}
	h.HandleLLMGenerateContentStart(ctx, nil) // Mistral stream returns without an end callback.
	h.HandleStreamingFunc(ctx, []byte("streamed"))
	h.EndRun(ctx)
	spans = exp.GetSpans()
	if len(spans) != 3 || spans[2].Status.Code == codes.Error || attr(spans[2].Attributes, "output.value") != "streamed" {
		t.Fatalf("stream ended incorrectly: %+v", spans)
	}
}
func TestCredentialValuesRedactedCompletely(t *testing.T) {
	for _, input := range []struct{ value, secret string }{
		{`{"password":"two words","query":"hi"}`, "two words"},
		{"Authorization: Basic c3ludGhldGlj", "c3ludGhldGlj"},
		{"Authorization: Bearer c3ludGhldGlj", "c3ludGhldGlj"},
		{`{"api_key":"spaced demo secret"}`, "spaced demo secret"},
	} {
		h, exp := setup(t)
		ctx := ContextWithRun(context.Background())
		h.HandleToolStart(ctx, input.value)
		h.HandleToolEnd(ctx, "ok")
		captured := attr(exp.GetSpans()[0].Attributes, "input.value")
		if strings.Contains(captured, input.secret) || strings.Contains(captured, "words") || !strings.Contains(captured, "[REDACTED]") {
			t.Fatalf("credential not fully redacted: %q", captured)
		}
	}
}
func TestNilLLMErrorCallbackMarksFailure(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleLLMGenerateContentEnd(ctx, nil)
	h.HandleLLMError(ctx, nil)
	spans := exp.GetSpans()
	if len(spans) != 1 || spans[0].Status.Code != codes.Error {
		t.Fatalf("nil-error callback incorrectly successful: %+v", spans)
	}
}
func TestProviderSpecificTokenUsage(t *testing.T) {
	for _, info := range []map[string]any{
		{"InputTokens": 11, "OutputTokens": 7},
		{"input_tokens": 11, "output_tokens": 7},
		{"usage": map[string]any{"prompt_tokens": 11, "completion_tokens": 7}},
		{"usage": struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
		}{11, 7}},
		{"usage": struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		}{11, 7, 18}},
	} {
		h, exp := setup(t)
		ctx := ContextWithRun(context.Background())
		h.HandleLLMGenerateContentStart(ctx, nil)
		h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "ok", GenerationInfo: info}}})
		attrs := exp.GetSpans()[0].Attributes
		if attr(attrs, "gen_ai.usage.input_tokens") != "11" || attr(attrs, "gen_ai.usage.output_tokens") != "7" {
			t.Fatalf("missing token count for %T: %+v", info["usage"], attrs)
		}
	}
}

func TestHiddenProvisionalResponseAndChainError(t *testing.T) {
	h, exp := setup(t)
	h.hideOutputs = true
	ctx := ContextWithRun(context.Background())
	h.HandleChainStart(ctx, nil)
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleLLMGenerateContentEnd(ctx, nil)
	h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "hidden"}}})
	h.HandleChainEnd(ctx, nil)
	h.HandleChainStart(ctx, nil)
	h.HandleLLMGenerateContentStart(ctx, nil)
	h.HandleChainError(ctx, errors.New("chain failed"))
	spans := exp.GetSpans()
	if len(spans) != 4 || spans[0].Status.Code == codes.Error || spans[1].Status.Code == codes.Error {
		t.Fatalf("valid hidden response marked failed: %+v", spans)
	}
	if spans[2].Status.Code != codes.Error || spans[3].Status.Code != codes.Error {
		t.Fatalf("failed chain not propagated: %+v", spans)
	}
}

func TestUnfinishedLLMThenNextCallIsSibling(t *testing.T) {
	h, exp := setup(t)
	ctx := ContextWithRun(context.Background())
	h.HandleChainStart(ctx, nil)
	h.HandleLLMGenerateContentStart(ctx, nil) // No end callback from Anthropic.
	h.HandleLLMGenerateContentStart(ctx, nil) // Next model call must not nest in the previous one.
	h.HandleLLMGenerateContentEnd(ctx, &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "ok"}}})
	h.HandleChainEnd(ctx, nil)
	spans := exp.GetSpans()
	if len(spans) != 3 || spans[0].Status.Code == codes.Error || spans[0].Parent.SpanID() != spans[2].SpanContext.SpanID() || spans[1].Parent.SpanID() != spans[2].SpanContext.SpanID() {
		t.Fatalf("model call incorrectly nested: %+v", spans)
	}
}
