package traceaigenai_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

type embeddingRoundTripFunc func(*http.Request) (*http.Response, error)

func (f embeddingRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestEmbeddingRESTPrivacyAndResponsePreservation(t *testing.T) {
	const input = `{"content":{"parts":[{"text":"private-document"},{"text":"private-thought","thought":true},{"inlineData":{"mimeType":"image/png","data":"private-image"}},{"fileData":{"mimeType":"image/png","fileUri":"https://private-file"}}]},"title":"private-title","taskType":"RETRIEVAL_DOCUMENT","outputDimensionality":2}`
	for _, batch := range []bool{false, true} {
		for name, tc := range map[string]traceaigenai.TraceConfig{
			"capture": {}, "inputs": {HideInputs: true}, "text": {HideInputText: true},
			"messages": {HideInputMessages: true}, "parameters": {HideLLMInvocationParameters: true}, "outputs": {HideOutputs: true},
		} {
			method, body, response := "embedContent", input, `{"embedding":{"values":[0.125,0.875]}}`
			if batch {
				method, body, response = "batchEmbedContents", `{"requests":[`+input+`]}`, `{"embeddings":[{"values":[0.125,0.875]}]}`
			}
			t.Run(method+"/"+name, func(t *testing.T) {
				rec := tracetest.NewSpanRecorder()
				tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
				defer tp.Shutdown(context.Background())
				base := embeddingRoundTripFunc(func(r *http.Request) (*http.Response, error) {
					got, _ := io.ReadAll(r.Body)
					if string(got) != body || r.Header.Get("Authorization") != "Bearer synthetic-bearer-token" {
						t.Error("request or credential changed")
					}
					return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(response)), Header: make(http.Header)}, nil
				})
				hc := &http.Client{Transport: base, Timeout: time.Second}
				wrapped := traceaigenai.WrapClient(hc, traceaigenai.WithTracerProvider(tp), traceaigenai.WithTraceConfig(tc))
				if wrapped.Timeout != hc.Timeout {
					t.Fatal("custom timeout lost")
				}
				req, _ := http.NewRequest(http.MethodPost, "http://localhost/v1beta/models/embed-model:"+method, strings.NewReader(body))
				req.Header.Set("Authorization", "Bearer synthetic-bearer-token")
				res, err := wrapped.Do(req)
				if err != nil {
					t.Fatal(err)
				}
				got, _ := io.ReadAll(res.Body)
				res.Body.Close()
				if string(got) != response {
					t.Fatal("response changed")
				}
				if len(rec.Ended()) != 1 {
					t.Fatalf("span count %d", len(rec.Ended()))
				}
				s := rec.Ended()[0]
				a := attrs(s)
				if a["embedding.count"] != "1" || a["gen_ai.embeddings.dimension.count"] != "2" {
					t.Fatal(a)
				}
				all := everything(s)
				for _, forbidden := range []string{"private-title", "private-thought", "private-image", "private-file", "0.125", "synthetic-bearer-token"} {
					if strings.Contains(all, forbidden) {
						t.Errorf("leaked %s", forbidden)
					}
				}
				if (tc.HideInputs || tc.HideInputText) && strings.Contains(all, "private-document") {
					t.Error("hidden input leaked")
				}
				if tc.HideInputs && a["input.value"] != traceaigenai.RedactedValue {
					t.Error("input summary not redacted")
				}
				if tc.HideInputs || tc.HideInputMessages {
					for k := range a {
						if strings.HasPrefix(k, "gen_ai.input.messages.") {
							t.Error("hidden input messages retained")
						}
					}
				}
				if tc.HideLLMInvocationParameters {
					if _, ok := a["gen_ai.request.parameters"]; ok {
						t.Error("hidden parameters retained")
					}
				} else if !strings.Contains(a["gen_ai.request.parameters"], "RETRIEVAL_DOCUMENT") {
					t.Error("missing task type")
				}
			})
		}
	}
}

func TestEmbeddingErrorsAndMalformedBodies(t *testing.T) {
	for _, tc := range []traceaigenai.TraceConfig{{}, {HideInputs: true}, {HideOutputs: true}} {
		for _, mode := range []string{"http", "transport", "canceled", "read", "malformed"} {
			t.Run(mode, func(t *testing.T) {
				rec := tracetest.NewSpanRecorder()
				tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
				defer tp.Shutdown(context.Background())
				base := embeddingRoundTripFunc(func(r *http.Request) (*http.Response, error) {
					switch mode {
					case "transport":
						return nil, errors.New("private-transport-url")
					case "canceled":
						return nil, context.Canceled
					case "read":
						return &http.Response{StatusCode: 200, Body: io.NopCloser(embeddingErrorReader{})}, nil
					case "http":
						return &http.Response{StatusCode: 400, Body: io.NopCloser(strings.NewReader(`{"error":{"status":"INVALID_ARGUMENT","message":"private-input synthetic-bearer-token"}}`))}, nil
					default:
						return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("not-json"))}, nil
					}
				})
				tr := traceaigenai.NewTransport(base, traceaigenai.WithTracerProvider(tp), traceaigenai.WithTraceConfig(tc))
				req, _ := http.NewRequest(http.MethodPost, "http://localhost/v1beta/models/embed-model:embedContent", strings.NewReader("bad request json"))
				req.Header.Set("Authorization", "Bearer synthetic-bearer-token")
				res, err := tr.RoundTrip(req)
				if mode == "transport" || mode == "canceled" {
					if err == nil {
						t.Fatal("error swallowed")
					}
				} else {
					b, readErr := io.ReadAll(res.Body)
					res.Body.Close()
					if mode == "read" && readErr == nil {
						t.Fatal("read error swallowed")
					}
					if mode == "malformed" && string(b) != "not-json" {
						t.Fatal("malformed response changed")
					}
				}
				if len(rec.Ended()) != 1 {
					t.Fatalf("span count %d", len(rec.Ended()))
				}
				s := rec.Ended()[0]
				all := everything(s)
				if mode != "malformed" && s.Status().Code != codes.Error {
					t.Error("missing error status")
				}
				if strings.Contains(all, "synthetic-bearer-token") || strings.Contains(all, "private-transport-url") {
					t.Error("credential or transport error leaked")
				}
				if (tc.HideInputs || tc.HideOutputs) && strings.Contains(all, "private-input") {
					t.Error("API error leaked hidden content")
				}
			})
		}
	}
}

type embeddingErrorReader struct{}

func (embeddingErrorReader) Read([]byte) (int, error) { return 0, errors.New("private-transport-url") }
