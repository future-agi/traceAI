package traceaia2a_test

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	collectortrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"
)

func TestErrorDetailsOTLPWire(t *testing.T) {
	for _, name := range []string{"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES"} {
		t.Setenv(name, "false")
	}
	t.Setenv("FI_HIDE_ERROR_DETAILS", "true")
	bodies := make(chan []byte, 1)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
		}
		bodies <- body
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer sink.Close()
	exporter, err := otlptracehttp.New(context.Background(), otlptracehttp.WithEndpointURL(sink.URL+"/tracer/v1/traces"))
	if err != nil {
		t.Fatal(err)
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter))
	defer tp.Shutdown(context.Background())
	tracing := traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp))
	rejection := a2a.NewError(a2a.ErrUnauthorized, "synthetic-private-error-92831")
	h := a2asrv.NewHandler(repliesWithMessage(), a2asrv.WithCallInterceptors(tracing, tracing.Wrap(responsePolicy{after: func(context.Context, *a2asrv.CallContext, *a2asrv.Response) error { return rejection }})))
	if _, got := h.SendMessage(context.Background(), userMessage("safe input")); got != rejection {
		t.Fatalf("error identity changed: %v", got)
	}
	if len(bodies) != 1 {
		t.Fatalf("want exactly one server-span export, got %d", len(bodies))
	}
	body := <-bodies
	if bytes.Contains(body, []byte("synthetic-private-error-92831")) {
		t.Fatal("FI_HIDE_ERROR_DETAILS=true leaked protocol error text in actual OTLP protobuf")
	}
	request := &collectortrace.ExportTraceServiceRequest{}
	if err := proto.Unmarshal(body, request); err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, resource := range request.ResourceSpans {
		for _, scope := range resource.ScopeSpans {
			for _, span := range scope.Spans {
				count++
				if span.Kind != tracepb.Span_SPAN_KIND_SERVER || span.Status.Code != tracepb.Status_STATUS_CODE_ERROR || span.Status.Message != "" {
					t.Fatalf("wrong exported error status: %v", span.Status)
				}
				values := map[string]string{}
				for _, attr := range span.Attributes {
					values[attr.Key] = attr.Value.GetStringValue()
				}
				if values[traceaia2a.ErrorType] != "UNAUTHORIZED" || values[traceaia2a.InputValue] != "safe input" {
					t.Fatal("error privacy dropped the class or unrelated input capture")
				}
			}
		}
	}
	if count != 1 {
		t.Fatalf("want one decoded server span, got %d", count)
	}
}
