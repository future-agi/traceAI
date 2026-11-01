package traceaia2a_test

import (
	"compress/gzip"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	collectortrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"
)

// Use the stock exporter, not a hand-built approximation of OTLP attributes.
// JSON is not a supported output format of otlptracehttp; it emits protobuf.
func TestServerOTLPWirePrivacy(t *testing.T) {
	for _, compression := range []otlptracehttp.Compression{otlptracehttp.NoCompression, otlptracehttp.GzipCompression} {
		name := "plain"
		if compression == otlptracehttp.GzipCompression {
			name = "gzip"
		}
		t.Run(name, func(t *testing.T) {
			requests := make(chan *collectortrace.ExportTraceServiceRequest, 8)
			sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v1/traces" || r.Header.Get("Content-Type") != "application/x-protobuf" {
					t.Error("wrong OTLP endpoint or content type")
				}
				var reader io.Reader = r.Body
				if compression == otlptracehttp.GzipCompression {
					if r.Header.Get("Content-Encoding") != "gzip" {
						t.Error("missing gzip header")
					}
					gz, err := gzip.NewReader(r.Body)
					if err != nil {
						t.Error(err)
						w.WriteHeader(400)
						return
					}
					defer gz.Close()
					reader = gz
				} else if r.Header.Get("Content-Encoding") != "" {
					t.Error("unexpected compression")
				}
				body, err := io.ReadAll(reader)
				if err != nil {
					t.Error(err)
					w.WriteHeader(400)
					return
				}
				if strings.Contains(string(body), marker) {
					t.Error("private content reached OTLP wire")
				}
				msg := &collectortrace.ExportTraceServiceRequest{}
				if err := proto.Unmarshal(body, msg); err != nil {
					t.Error(err)
					w.WriteHeader(400)
					return
				}
				requests <- msg
				w.Header().Set("Content-Type", "application/x-protobuf")
			}))
			defer sink.Close()
			exp, err := otlptracehttp.New(context.Background(), otlptracehttp.WithEndpointURL(sink.URL+"/v1/traces"), otlptracehttp.WithCompression(compression))
			if err != nil {
				t.Fatal(err)
			}
			tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
			defer func() { _ = tp.Shutdown(context.Background()) }()
			client := serverClient(t, "jsonrpc", completes(), tp, traceaia2a.TraceConfig{HideInputs: true, HideOutputs: true})
			for _, err := range client.SendStreamingMessage(context.Background(), userMessage(marker)) {
				if err != nil {
					t.Fatal(err)
				}
			}
			if _, err := client.GetTask(context.Background(), &a2a.GetTaskRequest{ID: "missing"}); err == nil {
				t.Fatal("expected protocol error")
			}
			if len(requests) != 4 {
				t.Fatalf("want two client/server pairs, got %d exports", len(requests))
			}
			byID := map[string]*tracepb.Span{}
			serverCount := 0
			for len(requests) > 0 {
				for _, rs := range (<-requests).ResourceSpans {
					for _, ss := range rs.ScopeSpans {
						if ss.Scope.Name != traceaia2a.ScopeName {
							t.Fatal("wrong instrumentation scope")
						}
						for _, s := range ss.Spans {
							byID[string(s.SpanId)] = s
							if s.Kind == tracepb.Span_SPAN_KIND_SERVER {
								serverCount++
							}
							if strings.Contains(s.String(), marker) {
								t.Fatal("private content in decoded OTLP")
							}
						}
					}
				}
			}
			if len(byID) != 4 || serverCount != 2 {
				t.Fatalf("unexpected wire span counts: %d total, %d server", len(byID), serverCount)
			}
			for _, s := range byID {
				if s.Kind != tracepb.Span_SPAN_KIND_SERVER {
					continue
				}
				p := byID[string(s.ParentSpanId)]
				if p == nil || p.Kind != tracepb.Span_SPAN_KIND_CLIENT || string(p.TraceId) != string(s.TraceId) {
					t.Fatal("OTLP parent chain broken")
				}
				if s.Name == "a2a.SendStreamingMessage" && (len(s.Events) != 4 || s.Status.Code != tracepb.Status_STATUS_CODE_OK) {
					t.Fatal("wire stream shape changed")
				}
				if s.Name == "a2a.GetTask" && s.Status.Code != tracepb.Status_STATUS_CODE_ERROR {
					t.Fatal("wire error status missing")
				}
			}
		})
	}
}
