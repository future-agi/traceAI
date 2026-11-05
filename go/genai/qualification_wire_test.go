package traceaigenai_test

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"google.golang.org/genai"
)

func TestQualificationDecodePreservesResponseBytes(t *testing.T) {
	for _, method := range []string{"generateContent", "streamGenerateContent"} {
		t.Run(method, func(t *testing.T) {
			qClearEnv(t)
			body := `synthetic-private-not-json`
			if method == "streamGenerateContent" {
				// A later valid chunk must not clear an earlier decode failure.
				body = "data: " + body + "\r\n\r\ndata: " + qBody + "\n\n"
			}
			exp := tracetest.NewInMemoryExporter()
			tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
			t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
			base := embeddingRoundTripFunc(func(r *http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: 200, Header: http.Header{"X-Fixture": {"unchanged"}}, Body: io.NopCloser(strings.NewReader(body))}, nil
			})
			transport := traceaigenai.NewTransport(base, traceaigenai.WithTracerProvider(tp))
			req, _ := http.NewRequest(http.MethodPost, "http://example.invalid/v1beta/models/test:"+method, strings.NewReader(`{"contents":[]}`))
			response, err := transport.RoundTrip(req)
			if err != nil {
				t.Fatal(err)
			}
			got, err := io.ReadAll(response.Body)
			if err != nil {
				t.Fatal(err)
			}
			_ = response.Body.Close()
			_ = response.Body.Close()
			if string(got) != body || response.Header.Get("X-Fixture") != "unchanged" || response.StatusCode != 200 {
				t.Fatal("instrumentation changed provider response")
			}
			s := qOne(t, exp)
			if s.Status.Code != codes.Error || qAttrs(s)["error.type"] != "decode" {
				t.Errorf("decode failure lost: %v %v", s.Status, qAttrs(s))
			}
			if strings.Contains(qAll(s), "synthetic-private-not-json") {
				t.Error("decode detail leaked")
			}
		})
	}
}

func TestQualificationInvalidStreamBlocks(t *testing.T) {
	for name, body := range map[string]string{
		"empty-data":    "data: \n\n",
		"invalid-event": "event: synthetic-private-event\n\n",
		"unterminated":  "data: synthetic-private-not-json",
		"after-valid":   "data: " + qBody + "\n\ndata: synthetic-private-not-json\n\n",
	} {
		t.Run(name, func(t *testing.T) {
			qClearEnv(t)
			c, _, exp := qSetup(t, func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = io.WriteString(w, body)
			}, true)
			if err := qGenerate(c, context.Background(), true, genai.Text(qRequest), nil); err == nil {
				t.Fatal("SDK accepted invalid stream")
			}
			s := qOne(t, exp)
			if s.Status.Code != codes.Error || qAttrs(s)["error.type"] != "decode" {
				t.Errorf("decode failure lost: %v", s.Status)
			}
			for _, secret := range []string{"synthetic-private-event", "synthetic-private-not-json"} {
				if strings.Contains(qAll(s), secret) {
					t.Error("decoder details exported")
				}
			}
		})
	}
}
