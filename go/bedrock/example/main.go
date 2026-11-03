package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	bedrock "github.com/future-agi/traceAI/go/bedrock"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	"go.opentelemetry.io/otel/sdk/trace"
)

func run() (runErr error) {
	for _, name := range []string{"FI_API_KEY", "FI_SECRET_KEY", "BEDROCK_MODEL_ID"} {
		if os.Getenv(name) == "" {
			return fmt.Errorf("set %s", name)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
	if endpoint == "" {
		endpoint = "https://api.futureagi.com/tracer/v1/traces"
	}
	exporter, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpointURL(endpoint),
		otlptracehttp.WithCompression(otlptracehttp.NoCompression),
		otlptracehttp.WithHeaders(map[string]string{
			"X-Api-Key":    os.Getenv("FI_API_KEY"),
			"X-Secret-Key": os.Getenv("FI_SECRET_KEY"),
		}),
	)
	if err != nil {
		return err
	}
	provider := trace.NewTracerProvider(trace.WithBatcher(exporter), trace.WithResource(resource.NewSchemaless(attribute.String("project_name", "bedrock-go"), attribute.String("project_type", "observe"))))
	defer func() {
		shutdown, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		runErr = errors.Join(runErr, provider.Shutdown(shutdown))
	}()
	awsConfig, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		return err
	}
	client := bedrock.NewClient(bedrockruntime.NewFromConfig(awsConfig), bedrock.WithTracerProvider(provider))
	ctx = bedrock.ContextWithSessionID(ctx, "weather-session")
	model := os.Getenv("BEDROCK_MODEL_ID")
	messages := []types.Message{{Role: types.ConversationRoleUser, Content: []types.ContentBlock{&types.ContentBlockMemberText{Value: "Suggest a short name for a weather app."}}}}
	response, err := client.Converse(ctx, &bedrockruntime.ConverseInput{ModelId: &model, Messages: messages, InferenceConfig: &types.InferenceConfiguration{MaxTokens: aws.Int32(128)}})
	if err != nil {
		return err
	}
	if message, ok := response.Output.(*types.ConverseOutputMemberMessage); ok {
		for _, part := range message.Value.Content {
			if text, ok := part.(*types.ContentBlockMemberText); ok {
				fmt.Println(text.Value)
			}
		}
	}
	streamed, err := client.ConverseStream(ctx, &bedrockruntime.ConverseStreamInput{ModelId: &model, Messages: messages, InferenceConfig: &types.InferenceConfiguration{MaxTokens: aws.Int32(128)}})
	if err != nil {
		return err
	}
	stream := streamed.GetStream()
	defer stream.Close()
	for event := range stream.Events() {
		if delta, ok := event.(*types.ConverseStreamOutputMemberContentBlockDelta); ok {
			if text, ok := delta.Value.Delta.(*types.ContentBlockDeltaMemberText); ok {
				fmt.Print(text.Value)
			}
		}
	}
	fmt.Println()
	return stream.Err()
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "Bedrock example failed:", err)
		os.Exit(1)
	}
}
