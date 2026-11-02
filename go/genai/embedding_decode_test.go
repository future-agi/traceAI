package traceaigenai_test

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"strings"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/codes"
	"google.golang.org/genai"
)

// Use the real SDK as the oracle: it normalizes JSON through a map before
// selecting the backend fields and decoding the full public response type.
func TestSDKEmbeddingDecoderParity(t *testing.T) {
	for _, tt := range []struct {
		name, body       string
		wantError        bool
		count, dimension int
	}{
		{"ordinary", `{"embeddings":[{"values":[0.25,0.75]}]}`, false, 1, 2},
		{"wrong vector type", `{"embeddings":[{"values":["synthetic-private-number"]}]}`, true, 0, 0},
		{"bad statistics", `{"embeddings":[{"values":[0.25,0.75],"statistics":{"tokenCount":"synthetic-private-number"}}]}`, true, 0, 0},
		{"bad truncated", `{"embeddings":[{"statistics":{"truncated":"synthetic-private-bool"}}]}`, true, 0, 0},
		{"bad token details", `{"embeddings":[{"statistics":{"tokensDetails":[{"tokenCount":"synthetic-private-number"}]}}]}`, true, 0, 0},
		{"bad metadata", `{"embeddings":[{"values":[0.25,0.75]}],"metadata":{"billableCharacterCount":"synthetic-private-number"}}`, true, 0, 0},
		{"metadata integer overflow", `{"embeddings":[],"metadata":{"billableCharacterCount":2147483648}}`, true, 0, 0},
		{"metadata fractional number", `{"metadata":{"billableCharacterCount":0.5}}`, true, 0, 0},
		{"ignored single embedding", `{"embeddings":[{"values":[0.25,0.75]}],"embedding":"synthetic-private-ignored"}`, false, 1, 2},
		{"ignored field overflow", `{"embeddings":[{"values":[0.25,0.75]}],"unknown":1e999}`, true, 0, 0},
		{"ignored nested overflow", `{"embeddings":[{"values":[0.25,0.75],"unknown":1e999}]}`, true, 0, 0},
		{"case sensitive top level", `{"EMBEDDINGS":[{"values":["synthetic-private-ignored"]}]}`, false, 0, 0},
		{"case sensitive metadata", `{"embeddings":[{"values":[0.25]}],"METADATA":"synthetic-private-ignored"}`, false, 1, 1},
		{"duplicate values key", `{"embeddings":[{"values":["synthetic-private-obsolete"],"values":[0.25]}]}`, false, 1, 1},
		{"duplicate embeddings key", `{"embeddings":"synthetic-private-obsolete","embeddings":[{"values":[0.25]}]}`, false, 1, 1},
		{"duplicate statistics key", `{"embeddings":[{"values":[0.25],"statistics":"synthetic-private-obsolete","statistics":{"tokenCount":2}}]}`, false, 1, 1},
		{"duplicate final invalid", `{"embeddings":[{"values":[0.25],"values":["synthetic-private-final"]}]}`, true, 0, 0},
		{"normalized integer exponent", `{"embeddings":[{"values":[0.25]}],"metadata":{"billableCharacterCount":1e2}}`, false, 1, 1},
		{"normalized integer decimal", `{"embeddings":[{"values":[0.25]}],"metadata":{"billableCharacterCount":2.0}}`, false, 1, 1},
		{"overwritten SDK response", `{"embeddings":[{"values":[0.25]}],"sdkHttpResponse":"synthetic-private-ignored"}`, false, 1, 1},
		{"null embedding element", `{"embeddings":[null,{"values":[0.25]}]}`, false, 2, 0},
		{"all null elements", `{"embeddings":[null,null]}`, false, 2, 0},
		{"null fields", `{"embeddings":null,"metadata":null}`, false, 0, 0},
		{"empty object", `{}`, false, 0, 0},
		{"empty body", ``, false, 0, 0},
		{"non object", `[]`, true, 0, 0},
		{"valid statistics and metadata", `{"embeddings":[{"values":[0.25,0.75],"statistics":{"truncated":true,"tokenCount":2,"tokensDetails":[{"modality":"TEXT","tokenCount":2}]}}],"metadata":{"billableCharacterCount":8}}`, false, 1, 2},
	} {
		t.Run(tt.name, func(t *testing.T) {
			clearHideEnv(t)
			tp, rec := regressionProvider(t)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if !strings.HasSuffix(r.URL.Path, ":batchEmbedContents") {
					t.Error("SDK used an unexpected endpoint")
				}
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Date", "Mon, 01 Jan 2024 00:00:00 GMT")
				_, _ = io.WriteString(w, tt.body)
			}))
			defer srv.Close()
			plain := regressionSDK(t, srv.Client(), srv.URL)
			plainResp, plainErr := plain.Models.EmbedContent(context.Background(), "gemini-embedding-001", genai.Text("synthetic-document"), nil)
			traced := regressionSDK(t, traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)), srv.URL)
			tracedResp, tracedErr := traced.Models.EmbedContent(context.Background(), "gemini-embedding-001", genai.Text("synthetic-document"), nil)
			if (plainErr != nil) != tt.wantError || (tracedErr != nil) != tt.wantError {
				t.Fatalf("want error=%v: plain=%v traced=%v", tt.wantError, plainErr, tracedErr)
			}
			if plainErr != nil && plainErr.Error() != tracedErr.Error() {
				t.Error("instrumentation changed the SDK error")
			}
			if !reflect.DeepEqual(plainResp, tracedResp) {
				t.Error("instrumentation changed the SDK response")
			}
			spans := rec.Ended()
			if len(spans) != 1 {
				t.Fatalf("spans=%d, want 1", len(spans))
			}
			s := spans[0]
			a := attrs(s)
			if (s.Status().Code == codes.Error) != tt.wantError {
				t.Errorf("SDK error=%v, span status=%+v", tracedErr, s.Status())
			}
			wantType := ""
			if tt.wantError {
				wantType = "decode"
			}
			if a["error.type"] != wantType || a["error.message"] != "" {
				t.Errorf("wrong error attributes: %v", a)
			}
			for key, want := range map[string]int{"embedding.count": tt.count, "gen_ai.embeddings.dimension.count": tt.dimension} {
				expected := ""
				if want > 0 {
					expected = strconv.Itoa(want)
				}
				if a[key] != expected {
					t.Errorf("%s=%q, want %q", key, a[key], expected)
				}
			}
			for _, forbidden := range []string{"synthetic-private-", "0.25", "0.75", "tokenCount", "billableCharacterCount", "statistics"} {
				if strings.Contains(everything(s), forbidden) {
					t.Errorf("response content leaked: %s", forbidden)
				}
			}
		})
	}
}

// The SDK's Gemini backend only calls the batch route. Cover direct REST
// consumers separately so the endpoint, not the response shape, selects fields.
func TestEmbeddingRESTDecoderRouteSelection(t *testing.T) {
	for _, tt := range []struct {
		name, method, body string
		wantError          bool
		count, dimension   string
	}{
		{"single ignores batch", "embedContent", `{"embedding":{"values":[0.25]},"embeddings":"synthetic-private-ignored"}`, false, "1", "1"},
		{"batch ignores single", "batchEmbedContents", `{"embedding":"synthetic-private-ignored","embeddings":[{"values":[0.25]}]}`, false, "1", "1"},
		{"single does not fall back", "embedContent", `{"embeddings":[{"values":[0.25]}]}`, false, "", ""},
		{"batch does not fall back", "batchEmbedContents", `{"embedding":{"values":[0.25]}}`, false, "", ""},
		{"single duplicate key", "embedContent", `{"embedding":{"values":["synthetic-private-obsolete"],"values":[0.25]}}`, false, "1", "1"},
		{"single case sensitive", "embedContent", `{"EMBEDDING":{"values":["synthetic-private-ignored"]}}`, false, "", ""},
		{"single invalid vector", "embedContent", `{"embedding":{"values":["synthetic-private-invalid"]}}`, true, "", ""},
		{"single vector overflow", "embedContent", `{"embedding":{"values":[1e100]}}`, true, "", ""},
		{"single ignored overflow", "embedContent", `{"embedding":{"values":[0.25]},"unknown":1e999}`, true, "", ""},
		{"single null", "embedContent", `{"embedding":null}`, false, "", ""},
		{"single empty", "embedContent", ``, false, "", ""},
		{"single malformed", "embedContent", `synthetic-private-not-json`, true, "", ""},
	} {
		t.Run(tt.name, func(t *testing.T) {
			clearHideEnv(t)
			tp, rec := regressionProvider(t)
			base := embeddingRoundTripFunc(func(*http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(tt.body)), Header: make(http.Header)}, nil
			})
			transport := traceaigenai.NewTransport(base, traceaigenai.WithTracerProvider(tp))
			req, err := http.NewRequest(http.MethodPost, "http://localhost/v1beta/models/embed-model:"+tt.method, strings.NewReader(`{}`))
			if err != nil {
				t.Fatal(err)
			}
			res, err := transport.RoundTrip(req)
			if err != nil {
				t.Fatal(err)
			}
			defer res.Body.Close()
			body, err := io.ReadAll(res.Body)
			if err != nil || string(body) != tt.body {
				t.Fatal("instrumentation changed the response body")
			}
			spans := rec.Ended()
			if len(spans) != 1 {
				t.Fatalf("spans=%d, want 1", len(spans))
			}
			s := spans[0]
			a := attrs(s)
			if (s.Status().Code == codes.Error) != tt.wantError {
				t.Errorf("status=%+v, want error=%v", s.Status(), tt.wantError)
			}
			if a["embedding.count"] != tt.count || a["gen_ai.embeddings.dimension.count"] != tt.dimension {
				t.Errorf("wrong metadata: %v", a)
			}
			if strings.Contains(everything(s), "synthetic-private-") || strings.Contains(everything(s), "0.25") {
				t.Error("response content leaked")
			}
		})
	}
}
