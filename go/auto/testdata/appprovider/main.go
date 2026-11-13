// An application with its own OpenTelemetry setup. It keeps a package-level
// tracer, as most Go services do, installs its own SDK provider in main with
// an exporter to its own collector, and makes one AI call inside an
// application span.
//
// Mode "global" makes that provider the global one and lets the agent
// activate on the first AI call. Mode "explicit" passes the provider to
// auto.Init before the first AI call instead.
package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// Initialized after the agent's package initialization and before main.
var tracer = otel.Tracer("svc")

func main() {
	ctx := context.Background()
	exporter, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(os.Args[2]+"/v1/traces"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exporter))
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = auto.Shutdown(ctx)
		_ = provider.Shutdown(ctx)
	}()
	application := tracer
	switch os.Args[3] {
	case "global":
		otel.SetTracerProvider(provider)
	case "explicit":
		if _, err := auto.Init(ctx, auto.WithTracerProvider(provider)); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		application = provider.Tracer("svc")
	}
	ctx, span := application.Start(ctx, "checkout", trace.WithAttributes(attribute.String("app.api_key", "sk-test-PLACEHOLDER")))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, os.Args[1]+"/v1/chat/completions", strings.NewReader(`{"model":"test-model","messages":[{"role":"user","content":"Hi"}]}`))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	b, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil {
		os.Exit(2)
	}
	span.End()
	_, _ = os.Stdout.Write(b)
}
