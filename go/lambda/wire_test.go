package traceailambda

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"
)

func wireProvider(t *testing.T, endpoint string) *sdktrace.TracerProvider {
	t.Helper()
	exporter, err := otlptracehttp.New(context.Background(),
		otlptracehttp.WithEndpointURL(endpoint+"/tracer/v1/traces"),
		otlptracehttp.WithHeaders(map[string]string{"X-Api-Key": "synthetic-key", "X-Secret-Key": "synthetic-secret-key"}),
		otlptracehttp.WithCompression(otlptracehttp.NoCompression),
		otlptracehttp.WithTimeout(FlushTimeout),
	)
	if err != nil {
		t.Fatal(err)
	}
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter, sdktrace.WithBatchTimeout(time.Hour)),
		sdktrace.WithResource(resource.NewSchemaless(
			attribute.String("project_name", "lambda-test"), attribute.String("project_type", "observe"), attribute.String("service.name", "lambda-test"),
		)),
	)
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = tp.Shutdown(ctx)
	})
	return tp
}

func TestOTLPWireLifecycleAndPrivacy(t *testing.T) {
	requests := make(chan *collector.ExportTraceServiceRequest, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			w.WriteHeader(500)
			return
		}
		if r.URL.Path != "/tracer/v1/traces" || r.Header.Get("Content-Type") != "application/x-protobuf" || r.Header.Get("Content-Encoding") != "" {
			t.Error("incorrect OTLP transport")
		}
		if r.Header.Get("X-Api-Key") != "synthetic-key" || r.Header.Get("X-Secret-Key") != "synthetic-secret-key" {
			t.Error("missing exporter authentication")
		}
		if strings.Contains(string(body), "synthetic-secret") {
			t.Error("OTLP body leaked content, headers, error, or panic text")
		}
		var req collector.ExportTraceServiceRequest
		if err := proto.Unmarshal(body, &req); err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		requests <- &req
		w.Header().Set("Content-Type", "application/x-protobuf")
		_, _ = w.Write([]byte{})
	}))
	defer server.Close()
	tp := wireProvider(t, server.URL)
	mode := "success"
	failure := errors.New("synthetic-secret")
	h := WrapHandler(handlerFunc(func(ctx context.Context, _ []byte) ([]byte, error) {
		_, child := tp.Tracer("app").Start(ctx, "lookup_order")
		child.End()
		switch mode {
		case "error":
			return nil, failure
		case "panic":
			panic(failure)
		}
		return []byte(`{"answer":"synthetic-secret"}`), nil
	}), tp)
	for i, current := range []string{"success", "error", "panic", "cancelled"} {
		mode = current
		ctx := invocationContext()
		if mode == "cancelled" {
			var cancel context.CancelFunc
			ctx, cancel = context.WithCancel(ctx)
			cancel()
		}
		func() {
			defer func() {
				if got := recover(); got != nil && (mode != "panic" || got != failure) {
					t.Errorf("unexpected panic: %T", got)
				}
			}()
			_, err := h.Invoke(ctx, []byte(httpPayload))
			if mode == "error" && err != failure {
				t.Error("application error changed")
			}
			if mode != "error" && err != nil {
				t.Error(err)
			}
		}()
		var req *collector.ExportTraceServiceRequest
		select {
		case req = <-requests:
		default:
			t.Fatalf("%s returned before flushing ended spans", mode)
		}
		if len(req.ResourceSpans) != 1 {
			t.Fatalf("unexpected resources: %d", len(req.ResourceSpans))
		}
		rs := req.ResourceSpans[0]
		resourceAttrs := map[string]string{}
		for _, attr := range rs.Resource.Attributes {
			resourceAttrs[attr.Key] = attr.Value.GetStringValue()
		}
		if resourceAttrs["project_name"] != "lambda-test" || resourceAttrs["project_type"] != "observe" || resourceAttrs["service.name"] != "lambda-test" {
			t.Fatal("Future AGI resource mapping lost")
		}
		var root, child *tracepb.Span
		count := 0
		for _, scope := range rs.ScopeSpans {
			for _, span := range scope.Spans {
				count++
				if span.Name == "lambda.handler" {
					root = span
				} else {
					child = span
				}
			}
		}
		if count != 2 || root == nil || child == nil || root.Kind != tracepb.Span_SPAN_KIND_SERVER {
			t.Fatalf("duplicate or missing invocation span: %d", count)
		}
		if string(root.SpanId) != string(child.ParentSpanId) || string(root.TraceId) != string(child.TraceId) {
			t.Fatal("wire trace linkage wrong")
		}
		coldFound := false
		for _, attr := range root.Attributes {
			if attr.Key == "faas.coldstart" {
				coldFound = true
				if attr.Value.GetBoolValue() != (i == 0) {
					t.Error("wire warm/cold state wrong")
				}
			}
		}
		if !coldFound || len(root.Events) != 0 {
			t.Fatal("missing cold-start marker or privacy event leak")
		}
		wantError := mode == "error" || mode == "panic"
		if (root.GetStatus().GetCode() == tracepb.Status_STATUS_CODE_ERROR) != wantError {
			t.Fatalf("wire error status incorrect for %s", mode)
		}
	}
}

func TestSlowCollectorCannotConsumeInvocationReserve(t *testing.T) {
	started := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		started <- struct{}{}
		select {
		case <-r.Context().Done():
		case <-time.After(2 * time.Second):
		}
	}))
	defer server.Close()
	tp := wireProvider(t, server.URL)
	h := WrapHandler(lambda.NewHandler(func() (string, error) { return "ok", nil }), tp)
	ctx, cancel := context.WithTimeout(invocationContext(), 750*time.Millisecond)
	defer cancel()
	start := time.Now()
	output, err := h.Invoke(ctx, []byte(`{}`))
	if err != nil || string(output) != `"ok"` {
		t.Fatal("collector timeout changed application result")
	}
	if time.Since(start) > 700*time.Millisecond {
		t.Fatalf("flush consumed deadline reserve: %s", time.Since(start))
	}
	select {
	case <-started:
	default:
		t.Fatal("flush was not attempted")
	}
}
