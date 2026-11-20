package traceaiopenai_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	traceaiopenai "github.com/future-agi/traceAI/go/openai"
	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestSupportedEndpointRoutingAndAttribution(t *testing.T) {
	for _, endpoint := range []struct{ method, path, op string }{
		{"POST", "/v1/responses", "generate_content"}, {"POST", "/openai/v1/responses/", "generate_content"},
		{"POST", "/openai/deployments/d/embeddings", "embeddings"},
		{"GET", "/v1/responses", ""}, {"POST", "/v1/responses/resp_1/cancel", ""}, {"POST", "/v1/responses/compact", ""},
		{"POST", "/v1/images/generations", ""}, {"POST", "/v1/audio/transcriptions", ""}, {"POST", "/v1/completions", ""},
	} {
		t.Run(endpoint.method+endpoint.path, func(t *testing.T) {
			rec := tracetest.NewSpanRecorder()
			tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
			defer tp.Shutdown(context.Background())
			mw := traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp))
			requestBody := `{"model":"fixture","input":"hello"}`
			req, _ := http.NewRequest(endpoint.method, "https://resource.openai.azure.com"+endpoint.path+"?api-version=x", strings.NewReader(requestBody))
			called := false
			res, err := mw(req, func(r *http.Request) (*http.Response, error) {
				called = true
				body, _ := io.ReadAll(r.Body)
				if string(body) != requestBody {
					t.Error("request changed")
				}
				return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
			})
			if err != nil || !called {
				t.Fatal("next not called")
			}
			defer res.Body.Close()
			if endpoint.op == "" {
				if len(rec.Started()) != 0 {
					t.Error("unsupported endpoint spanned")
				}
				return
			}
			a := attrs(onlySpan(t, rec))
			if str(a, traceaiopenai.GenAIOperationName) != endpoint.op || str(a, traceaiopenai.GenAIProviderName) != "azure" {
				t.Error("operation/provider missing")
			}
			for _, key := range []string{traceaiopenai.GenAIUsageInputTokens, traceaiopenai.GenAIUsageOutputTokens, traceaiopenai.GenAIUsageTotalTokens} {
				if _, exists := a[key]; exists {
					t.Errorf("invented %s", key)
				}
			}
		})
	}
}

func TestResponsesInputItemsAndPrivacy(t *testing.T) {
	input := []map[string]any{
		{"role": "user", "content": []map[string]string{{"type": "input_text", "text": marker}, {"type": "input_image", "image_url": "data:image/png;base64," + strings.Repeat("a", 100)}, {"type": "input_file", "file_data": "file-never-captured"}}},
		{"type": "function_call", "call_id": "prev", "name": "lookup", "arguments": marker},
		{"type": "function_call_output", "call_id": "prev", "output": marker},
		{"type": "reasoning", "encrypted_content": "encrypted-never-captured"},
	}
	for _, hidden := range []bool{false, true} {
		client, rec, _ := extendedClient(t, func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			for _, want := range []string{marker, "file-never-captured", "encrypted-never-captured", "tool-credential-never-captured"} {
				if !strings.Contains(string(body), want) {
					t.Errorf("SDK request lost %s", want)
				}
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, responseJSON)
		}, traceaiopenai.TraceConfig{HideInputs: hidden, HideOutputs: hidden, Base64ImageMaxLength: 32})
		_, err := client.Responses.New(context.Background(), responseParams(), option.WithJSONSet("input", input), option.WithJSONSet("tools", []map[string]any{
			{"type": "function", "name": "lookup", "description": marker, "parameters": map[string]string{"type": "object"}},
			{"type": "mcp", "authorization": "tool-credential-never-captured"},
		}), option.WithJSONSet("metadata", map[string]string{"secret": "metadata-never-captured"}))
		if err != nil {
			t.Fatal(err)
		}
		s := onlySpan(t, rec)
		for _, leak := range []string{"file-never-captured", "encrypted-never-captured", "tool-credential-never-captured", "metadata-never-captured", strings.Repeat("a", 100)} {
			if strings.Contains(allText(s), leak) {
				t.Errorf("leaked %s", leak)
			}
		}
		if hidden {
			if strings.Contains(allText(s), marker) {
				t.Error("hidden content leaked")
			}
			continue
		}
		a := attrs(s)
		for key, want := range map[string]string{
			"gen_ai.input.messages.0.message.content":                                   "instruction-" + marker,
			"gen_ai.input.messages.1.message.contents.0.message_content.text":           marker,
			"gen_ai.input.messages.1.message.contents.1.message_content.image":          traceaiopenai.RedactedValue,
			"gen_ai.input.messages.2.message.tool_calls.0.tool_call.function.arguments": marker,
			"gen_ai.input.messages.3.message.content":                                   marker,
		} {
			if str(a, key) != want {
				t.Errorf("%s = %q, want %q", key, str(a, key), want)
			}
		}
	}
}

func TestExtendedHTTPFailuresAndRetries(t *testing.T) {
	for _, endpoint := range []string{"responses", "embeddings"} {
		t.Run(endpoint, func(t *testing.T) {
			attempts := 0
			client, rec, tp := extendedClient(t, func(w http.ResponseWriter, r *http.Request) {
				attempts++
				w.Header().Set("Content-Type", "application/json")
				if attempts == 1 {
					w.WriteHeader(429)
					_, _ = io.WriteString(w, `{"error":{"type":"rate_limit_error","message":"`+marker+`"}}`)
					return
				}
				if endpoint == "responses" {
					_, _ = io.WriteString(w, responseJSON)
				} else {
					_, _ = io.WriteString(w, embeddingJSON)
				}
			}, traceaiopenai.TraceConfig{HideInputs: true, HideOutputs: true})
			ctx, parent := tp.Tracer("caller").Start(context.Background(), "parent")
			var err error
			if endpoint == "responses" {
				_, err = client.Responses.New(ctx, responseParams(), option.WithMaxRetries(1))
			} else {
				_, err = client.Embeddings.New(ctx, openai.EmbeddingNewParams{Model: "text-embedding-3-small", Input: openai.EmbeddingNewParamsInputUnion{OfString: openai.String(marker)}, EncodingFormat: openai.EmbeddingNewParamsEncodingFormatFloat}, option.WithMaxRetries(1))
			}
			if err != nil {
				t.Fatal(err)
			}
			spans := rec.Ended()
			if len(spans) != 2 {
				t.Fatalf("spans=%d", len(spans))
			}
			if spans[0].Status().Code != codes.Error || str(attrs(spans[0]), traceaiopenai.ErrorType) != "rate_limit_error" || str(attrs(spans[1]), traceaiopenai.HTTPRequestResendCount) != "1" {
				t.Error("retry/error semantics lost")
			}
			for _, s := range spans {
				if s.Parent().SpanID() != parent.SpanContext().SpanID() || strings.Contains(allText(s), marker) {
					t.Error("parent/privacy lost")
				}
			}
			parent.End()
		})
	}
}

func TestResponsesJSONFailureIsNotSuccess(t *testing.T) {
	for _, status := range []string{"failed", "incomplete"} {
		client, rec, _ := extendedClient(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"id":"resp_failure","status":"`+status+`","error":{"code":"server_error","message":"`+marker+`"}}`)
		}, traceaiopenai.TraceConfig{HideInputs: true})
		res, err := client.Responses.New(context.Background(), responseParams())
		if err != nil || string(res.Status) != status {
			t.Fatalf("SDK result changed: %v %v", res, err)
		}
		s := onlySpan(t, rec)
		if s.Status().Code != codes.Error || strings.Contains(allText(s), marker) {
			t.Error("failed response status/privacy incorrect")
		}
	}
}

func TestExtendedTransportErrors(t *testing.T) {
	for _, path := range []string{"responses", "embeddings"} {
		rec := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
		mw := traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp))
		req, _ := http.NewRequest("POST", "https://api.openai.com/v1/"+path, strings.NewReader(`{"model":"fixture","input":"`+marker+`"}`))
		errWant := errors.New("dial https://private-host/" + marker)
		_, err := mw(req, func(*http.Request) (*http.Response, error) { return nil, errWant })
		if err != errWant {
			t.Error("transport error changed")
		}
		s := onlySpan(t, rec)
		if s.Status().Code != codes.Error || str(attrs(s), traceaiopenai.ErrorType) != "transport" || strings.Contains(allText(s), "private-host") {
			t.Error("transport status/privacy incorrect")
		}
		_ = tp.Shutdown(context.Background())
	}
}
