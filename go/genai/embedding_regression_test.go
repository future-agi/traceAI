package traceaigenai_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"google.golang.org/genai"
)

func regressionSDK(t *testing.T, hc *http.Client, url string) *genai.Client {
	t.Helper()
	c, err := genai.NewClient(context.Background(), &genai.ClientConfig{
		APIKey: testGeminiKey, Backend: genai.BackendGeminiAPI,
		HTTPClient: hc, HTTPOptions: genai.HTTPOptions{BaseURL: url + "/"},
	})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func regressionProvider(t *testing.T) (*sdktrace.TracerProvider, *tracetest.SpanRecorder) {
	t.Helper()
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return tp, rec
}

func TestSDKErrorPrivacy(t *testing.T) {
	for _, operation := range []string{"embedding", "generation", "stream"} {
		for _, flag := range []string{
			"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT",
			"FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_ERROR_DETAILS",
		} {
			t.Run(operation+"/"+flag, func(t *testing.T) {
				clearHideEnv(t)
				t.Setenv(flag, "true")
				tp, rec := regressionProvider(t)
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					b, _ := io.ReadAll(r.Body)
					if !strings.Contains(string(b), "synthetic-private-document") {
						t.Error("SDK request missing input")
					}
					if operation == "stream" {
						w.Header().Set("Content-Type", "text/event-stream")
					} else {
						w.Header().Set("Content-Type", "application/json")
						w.WriteHeader(http.StatusBadRequest)
					}
					_, _ = io.WriteString(w, "{\"error\":{\"code\":400,\"status\":\"INVALID_ARGUMENT\",\"message\":\"Rejected synthetic-private-document; synthetic-private-error-detail\"}}\n\n")
				}))
				defer srv.Close()
				c := regressionSDK(t, traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)), srv.URL)
				var err error
				switch operation {
				case "embedding":
					_, err = c.Models.EmbedContent(context.Background(), "gemini-embedding-001", genai.Text("synthetic-private-document"), nil)
				case "generation":
					_, err = c.Models.GenerateContent(context.Background(), "gemini-flash", genai.Text("synthetic-private-document"), nil)
				case "stream":
					for _, streamErr := range c.Models.GenerateContentStream(context.Background(), "gemini-flash", genai.Text("synthetic-private-document"), nil) {
						err = streamErr
					}
				}
				var apiErr genai.APIError
				if !errors.As(err, &apiErr) || apiErr.Code != 400 || !strings.Contains(apiErr.Message, "synthetic-private-document") {
					t.Fatalf("SDK error must remain unchanged: %v", err)
				}
				if len(rec.Ended()) != 1 {
					t.Fatalf("spans=%d", len(rec.Ended()))
				}
				s := rec.Ended()[0]
				if s.Status().Code != codes.Error || attrs(s)["error.type"] != "INVALID_ARGUMENT" {
					t.Errorf("lost error classification: %v %v", s.Status(), attrs(s))
				}
				if attrs(s)["error.message"] != "" || strings.Contains(s.Status().Description, "synthetic-private-") {
					t.Error("hidden error details exported in error.message / status.description")
				}
			})
		}
	}
}

func TestSDKEmbeddingDoubleWrapping(t *testing.T) {
	clearHideEnv(t)
	tp, rec := regressionProvider(t)
	var requests atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"embeddings":[{"values":[0.25,0.75]}]}`)
	}))
	defer srv.Close()
	first := traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp))
	// Omitted options inherit the provider; an explicit config replaces privacy.
	second := traceaigenai.WrapClient(first, traceaigenai.WithTraceConfig(traceaigenai.TraceConfig{HideInputs: true}))
	third := traceaigenai.WrapClient(second)
	for _, hc := range []*http.Client{second, third, first} {
		c := regressionSDK(t, hc, srv.URL)
		resp, err := c.Models.EmbedContent(context.Background(), "gemini-embedding-001", genai.Text("synthetic-private-document"), nil)
		if err != nil || len(resp.Embeddings) != 1 || resp.Embeddings[0].Values[0] != 0.25 {
			t.Fatalf("SDK result changed: %v, %v", resp, err)
		}
	}
	spans := rec.Ended()
	if requests.Load() != 3 || len(spans) != 3 {
		t.Fatalf("HTTP attempts=%d spans=%d, want one span per attempt", requests.Load(), len(spans))
	}
	for _, s := range spans[:2] {
		if strings.Contains(everything(s), "synthetic-private-document") || attrs(s)["input.value"] != traceaigenai.RedactedValue {
			t.Error("outer HideInputs bypassed")
		}
	}
	if !strings.Contains(attrs(spans[2])["input.value"], "synthetic-private-document") {
		t.Error("wrapping mutated the original client privacy settings")
	}
}

func TestSDKEmbeddingDecodeFailuresAreErrors(t *testing.T) {
	for _, tt := range []struct{ name, body string }{
		{"malformed JSON", `synthetic-private-not-json`},
		{"wrong vector type", `{"embeddings":[{"values":["synthetic-private-not-a-float"]}]}`},
		{"float32 overflow", `{"embeddings":[{"values":[1e100]}]}`},
	} {
		t.Run(tt.name, func(t *testing.T) {
			clearHideEnv(t)
			tp, rec := regressionProvider(t)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Date", "Mon, 01 Jan 2024 00:00:00 GMT")
				_, _ = io.WriteString(w, tt.body)
			}))
			defer srv.Close()
			// Compare the SDK's original error to the instrumented call's error.
			plain := regressionSDK(t, srv.Client(), srv.URL)
			_, plainErr := plain.Models.EmbedContent(context.Background(), "gemini-embedding-001", genai.Text("synthetic-document"), nil)
			c := regressionSDK(t, traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)), srv.URL)
			_, err := c.Models.EmbedContent(context.Background(), "gemini-embedding-001", genai.Text("synthetic-document"), nil)
			if err == nil || plainErr == nil {
				t.Fatalf("SDK should reject both responses: plain=%v traced=%v", plainErr, err)
			}
			if err.Error() != plainErr.Error() {
				t.Errorf("instrumentation changed SDK error: plain=%v traced=%v", plainErr, err)
			}
			if len(rec.Ended()) != 1 {
				t.Fatalf("spans=%d", len(rec.Ended()))
			}
			s := rec.Ended()[0]
			if s.Status().Code != codes.Error || attrs(s)["error.type"] != "decode" {
				t.Errorf("SDK call failed but span lacks decode error: %v %v", s.Status(), attrs(s))
			}
			if strings.Contains(everything(s), "synthetic-private-") || attrs(s)["error.message"] != "" {
				t.Error("decode failure leaked response content")
			}
		})
	}
}

func TestSDKErrorPrivacyOverride(t *testing.T) {
	for _, mode := range []string{"env false", "env true", "config false", "config true"} {
		t.Run(mode, func(t *testing.T) {
			clearHideEnv(t)
			t.Setenv("FI_HIDE_INPUT_TEXT", "true")
			hide := strings.HasSuffix(mode, "true")
			var opts []traceaigenai.Option
			if strings.HasPrefix(mode, "env") {
				t.Setenv("FI_HIDE_ERROR_DETAILS", strings.TrimPrefix(mode, "env "))
			} else {
				// An explicit config must win over the opposite env setting.
				t.Setenv("FI_HIDE_ERROR_DETAILS", "true")
				if hide {
					t.Setenv("FI_HIDE_ERROR_DETAILS", "false")
				}
				opts = append(opts, traceaigenai.WithTraceConfig(traceaigenai.TraceConfig{HideInputText: true, HideErrorDetails: &hide}))
			}
			h := newHarness(t, opts...)
			_, err := h.client.Models.GenerateContent(context.Background(), "fixture-404", genai.Text("synthetic-private-document"), nil)
			if err == nil {
				t.Fatal("SDK should reject response")
			}
			s := h.only()
			hasMessage := attrs(s)["error.message"] != ""
			hasStatusMessage := strings.Contains(s.Status().Description, "models/no-such-model-th8273 is not found")
			if hasMessage == hide || hasStatusMessage == hide {
				t.Errorf("hide=%v: attributes=%v status=%v", hide, attrs(s), s.Status())
			}
		})
	}
}

func TestSDKEmbeddingEmptyResponsesAreNotErrors(t *testing.T) {
	for _, body := range []string{"", `{}`, `{"embeddings":[]}`, `{"embeddings":[{"values":[]}]}`, `{"unknown":"synthetic-private-detail"}`} {
		t.Run(body, func(t *testing.T) {
			clearHideEnv(t)
			tp, rec := regressionProvider(t)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				_, _ = io.WriteString(w, body)
			}))
			defer srv.Close()
			c := regressionSDK(t, traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)), srv.URL)
			if _, err := c.Models.EmbedContent(context.Background(), "gemini-embedding-001", genai.Text("synthetic-document"), nil); err != nil {
				t.Fatal(err)
			}
			if len(rec.Ended()) != 1 {
				t.Fatalf("spans=%d", len(rec.Ended()))
			}
			s := rec.Ended()[0]
			if s.Status().Code == codes.Error || attrs(s)["error.type"] != "" {
				t.Errorf("successful SDK call recorded as failure: %v", s.Status())
			}
			if strings.Contains(everything(s), "synthetic-private-") {
				t.Error("unknown response field captured")
			}
		})
	}
}
