// Package traceai_openai instruments the OpenAI Go client with OTel tracing.
//
// Usage:
//
//	provider, _ := traceai.Register()
//	defer provider.Shutdown(context.Background())
//
//	client := openai.NewClient(
//	    option.WithMiddleware(traceai_openai.Middleware()),
//	)
package traceai_openai

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/future-agi/traceAI/go/traceai"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

const instrumentationName = "traceai_openai"
const instrumentationVersion = "0.1.0"

// Middleware returns an openai-go compatible middleware that records
// LLM calls as OTel spans.
func Middleware(opts ...Option) func(req *http.Request, next func(req *http.Request) (*http.Response, error)) (*http.Response, error) {
	cfg := defaultOptions()
	for _, o := range opts {
		o(&cfg)
	}

	tp := cfg.tracerProvider
	if tp == nil {
		tp = otel.GetTracerProvider()
	}

	tracer := tp.Tracer(
		instrumentationName,
		trace.WithInstrumentationVersion(instrumentationVersion),
	)

	return func(req *http.Request, next func(req *http.Request) (*http.Response, error)) (*http.Response, error) {
		opName := operationFromPath(req.URL.Path)
		if opName == "" {
			return next(req)
		}

		spanName := "openai." + opName
		ctx, span := tracer.Start(req.Context(), spanName,
			trace.WithSpanKind(trace.SpanKindClient),
		)
		defer span.End()

		span.SetAttributes(
			traceai.AttrGenAISystem.String(traceai.GenAISystemOpenAI),
			traceai.AttrGenAIOperationName.String(opName),
		)

		if req.Body != nil && cfg.captureContent {
			bodyBytes, err := io.ReadAll(req.Body)
			if err == nil {
				req.Body = io.NopCloser(strings.NewReader(string(bodyBytes)))
				extractRequestAttributes(span, bodyBytes)
			}
		}

		req = req.WithContext(ctx)
		start := time.Now()

		resp, err := next(req)

		span.SetAttributes(attribute.Float64("gen_ai.request.duration_ms",
			float64(time.Since(start).Milliseconds())))

		if err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
			return resp, err
		}

		if resp != nil && resp.StatusCode >= 400 {
			span.SetStatus(codes.Error, resp.Status)
			span.SetAttributes(attribute.Int("http.response.status_code", resp.StatusCode))
			return resp, err
		}

		// read response for token counts
		if resp != nil && resp.Body != nil && cfg.captureContent {
			bodyBytes, readErr := io.ReadAll(resp.Body)
			if readErr == nil {
				resp.Body = io.NopCloser(strings.NewReader(string(bodyBytes)))
				extractResponseAttributes(span, bodyBytes)
			}
		}

		span.SetStatus(codes.Ok, "")
		return resp, err
	}
}

func operationFromPath(path string) string {
	switch {
	case strings.Contains(path, "/chat/completions"):
		return traceai.OpChat
	case strings.Contains(path, "/completions"):
		return traceai.OpCompletion
	case strings.Contains(path, "/embeddings"):
		return traceai.OpEmbedding
	default:
		return ""
	}
}

type chatRequest struct {
	Model       string          `json:"model"`
	Messages    json.RawMessage `json:"messages"`
	MaxTokens   *int            `json:"max_tokens,omitempty"`
	Temperature *float64        `json:"temperature,omitempty"`
	TopP        *float64        `json:"top_p,omitempty"`
}

func extractRequestAttributes(span trace.Span, body []byte) {
	var req chatRequest
	if err := json.Unmarshal(body, &req); err != nil {
		return
	}

	if req.Model != "" {
		span.SetAttributes(traceai.AttrGenAIRequestModel.String(req.Model))
	}
	if req.MaxTokens != nil {
		span.SetAttributes(traceai.AttrGenAIRequestMaxTokens.Int(*req.MaxTokens))
	}
	if req.Temperature != nil {
		span.SetAttributes(traceai.AttrGenAIRequestTemperature.Float64(*req.Temperature))
	}
	if req.TopP != nil {
		span.SetAttributes(traceai.AttrGenAIRequestTopP.Float64(*req.TopP))
	}
	if len(req.Messages) > 0 {
		span.SetAttributes(traceai.AttrGenAIPrompt.String(string(req.Messages)))
	}
}

type chatResponse struct {
	ID      string `json:"id"`
	Model   string `json:"model"`
	Choices []struct {
		FinishReason string `json:"finish_reason"`
		Message      struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
	} `json:"usage"`
}

func extractResponseAttributes(span trace.Span, body []byte) {
	var resp chatResponse
	if err := json.Unmarshal(body, &resp); err != nil {
		return
	}

	if resp.ID != "" {
		span.SetAttributes(traceai.AttrGenAIResponseID.String(resp.ID))
	}
	if resp.Model != "" {
		span.SetAttributes(traceai.AttrGenAIResponseModel.String(resp.Model))
	}
	if resp.Usage.PromptTokens > 0 {
		span.SetAttributes(traceai.AttrGenAIUsageInputTokens.Int(resp.Usage.PromptTokens))
	}
	if resp.Usage.CompletionTokens > 0 {
		span.SetAttributes(traceai.AttrGenAIUsageOutputTokens.Int(resp.Usage.CompletionTokens))
	}

	if len(resp.Choices) > 0 {
		reasons := make([]string, 0, len(resp.Choices))
		var completions []string
		for _, c := range resp.Choices {
			if c.FinishReason != "" {
				reasons = append(reasons, c.FinishReason)
			}
			if c.Message.Content != "" {
				completions = append(completions, c.Message.Content)
			}
		}
		if len(reasons) > 0 {
			span.SetAttributes(traceai.AttrGenAIResponseFinishReasons.StringSlice(reasons))
		}
		if len(completions) > 0 {
			span.SetAttributes(traceai.AttrGenAICompletion.String(strings.Join(completions, "\n")))
		}
	}
}
