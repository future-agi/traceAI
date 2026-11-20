// Offline helper for the ambient OTLP header test. Mode "init" configures the
// agent with an explicit auto.Init; mode "zero" is environment-only and
// activates on the first AI request. Both emit one module span and flush.
package main

import (
	"context"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

func main() {
	otel.SetTracerProvider(sdktrace.NewTracerProvider())
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	defer func() { _ = auto.Shutdown(ctx) }()
	if os.Args[2] == "init" {
		if _, err := auto.Init(ctx); err != nil {
			os.Exit(1)
		}
	} else {
		resp, err := http.Post(os.Args[1]+"/v1/chat/completions", "application/json", strings.NewReader(`{"model":"test-model","messages":[{"role":"user","content":"Hi"}]}`))
		if err != nil {
			os.Exit(2)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
	}
	_, span := otel.Tracer("application").Start(ctx, "ambient-header-span", trace.WithAttributes(attribute.String("gen_ai.span.kind", "CHAIN")))
	span.End()
}
