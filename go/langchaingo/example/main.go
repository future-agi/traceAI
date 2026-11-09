// Run with FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME and OPENAI_API_KEY set.
package main

import (
	"context"
	"fmt"
	"os"

	traceailangchaingo "github.com/future-agi/traceAI/go/langchaingo"
	"github.com/tmc/langchaingo/llms"
	"github.com/tmc/langchaingo/llms/openai"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func run(ctx context.Context) error {
	exp, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL("https://api.futureagi.com/tracer/v1/traces"), otlptracehttp.WithHeaders(map[string]string{"X-Api-Key": os.Getenv("FI_API_KEY"), "X-Secret-Key": os.Getenv("FI_SECRET_KEY")}), otlptracehttp.WithCompression(otlptracehttp.NoCompression))
	if err != nil {
		return err
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(resource.NewSchemaless(attribute.String("project_name", os.Getenv("FI_PROJECT_NAME")), attribute.String("project_type", "observe"))))
	defer tp.Shutdown(context.Background())
	handler := traceailangchaingo.NewHandler(traceailangchaingo.WithTracerProvider(tp))
	model, err := openai.New(openai.WithCallback(handler))
	if err != nil {
		return err
	}
	ctx = traceailangchaingo.ContextWithRun(ctx)
	defer handler.EndRun(ctx)
	response, err := llms.GenerateFromSinglePrompt(ctx, model, "Name one landmark in Kyoto.")
	if err != nil {
		return err
	}
	fmt.Println(response)
	return nil
}
func main() {
	if err := run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
