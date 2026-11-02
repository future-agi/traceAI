// This example configures one provider for the execution environment. The
// Lambda runtime supplies invocation contexts; no AWS API client is needed.
package main

import (
	"cmp"
	"context"
	"errors"
	"log"
	"os"
	"strings"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	traceailambda "github.com/future-agi/traceAI/go/lambda"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func main() {
	name := cmp.Or(os.Getenv("FI_PROJECT_NAME"), os.Getenv("AWS_LAMBDA_FUNCTION_NAME"), "aws-lambda")
	attrs := []attribute.KeyValue{
		attribute.String("project_name", name),
		attribute.String("project_type", "observe"),
		attribute.String("service.name", name),
	}
	if function := os.Getenv("AWS_LAMBDA_FUNCTION_NAME"); function != "" {
		attrs = append(attrs, attribute.String("faas.name", function))
	}
	opts := []sdktrace.TracerProviderOption{sdktrace.WithResource(resource.NewSchemaless(attrs...))}
	key, secret := os.Getenv("FI_API_KEY"), os.Getenv("FI_SECRET_KEY")
	if key != "" && secret != "" {
		base := cmp.Or(strings.TrimRight(os.Getenv("FI_BASE_URL"), "/"), "https://api.futureagi.com")
		exporter, err := otlptracehttp.New(context.Background(),
			otlptracehttp.WithEndpointURL(base+"/tracer/v1/traces"),
			otlptracehttp.WithHeaders(map[string]string{"X-Api-Key": key, "X-Secret-Key": secret}),
			otlptracehttp.WithCompression(otlptracehttp.NoCompression),
			otlptracehttp.WithTimeout(traceailambda.FlushTimeout),
		)
		if err == nil {
			opts = append(opts, sdktrace.WithBatcher(exporter, sdktrace.WithBatchTimeout(time.Minute)))
		} else {
			log.Print("tracing disabled: could not configure OTLP exporter")
		}
	} else {
		log.Print("tracing disabled: set both FI_API_KEY and FI_SECRET_KEY")
	}
	provider := sdktrace.NewTracerProvider(opts...)
	otel.SetTracerProvider(provider)
	lambda.StartHandler(traceailambda.WrapHandler(lambda.NewHandler(handle), provider))
}

type greeting struct {
	Name string `json:"name"`
}

func handle(_ context.Context, event greeting) (string, error) {
	if event.Name == "" {
		return "", errors.New("name is required")
	}
	return "Hello, " + event.Name, nil
}
