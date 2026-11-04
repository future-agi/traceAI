// Command example sends a small tool-using weather agent's Anthropic Messages
// calls to Future AGI.
//
// It exports with the stock OpenTelemetry OTLP/HTTP exporter, installs the
// traceAI middleware on the Anthropic client, and opens an agent span and a tool
// span of its own, so a run is one trace:
//
//	weather-agent                  AGENT (this program)
//	├── anthropic.messages.create  LLM   (asks for the get_weather tool)
//	├── execute_tool get_weather   TOOL  (this program)
//	└── anthropic.messages.create  LLM   (final answer, streamed with -stream)
//
// Environment:
//
//	ANTHROPIC_API_KEY  read by anthropic-sdk-go (as are ANTHROPIC_BASE_URL and the rest of its variables)
//	FI_API_KEY         Future AGI API key
//	FI_SECRET_KEY      Future AGI secret key
//	FI_PROJECT_NAME    Observe project (created on the first export)
//	FI_BASE_URL        optional, default https://api.futureagi.com
//	FI_HIDE_*          optional, the traceAI content settings
//
// Run it with go run ./example from the module directory.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"strings"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/option"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"

	traceaianthropic "github.com/future-agi/traceAI/go/anthropic"
)

func main() {
	model := flag.String("model", "claude-sonnet-5-5", "model to call")
	question := flag.String("question", "What is the weather in Paris right now? Answer in one sentence.", "question for the agent")
	session := flag.String("session", "", "session id for the run (optional)")
	stream := flag.Bool("stream", false, "stream the final answer")
	fail := flag.Bool("fail", false, "also make one call that the API rejects, to see an error span")
	flag.Parse()
	if err := run(*model, *question, *session, *stream, *fail); err != nil {
		log.Fatal(err)
	}
}

func run(model, question, session string, stream, fail bool) error {
	ctx := context.Background()

	// 1. Export to Future AGI: both keys as headers, project_name as a resource attribute.
	tp, err := newTracerProvider(ctx)
	if err != nil {
		return err
	}
	// Flush the batch before the process exits, or a short run exports nothing.
	defer func() {
		if err := tp.Shutdown(context.Background()); err != nil {
			log.Printf("trace export: %v", err)
		}
	}()
	// anthropic-sdk-go opens its span through the registered provider, and the
	// middleware adds Future AGI's attributes to it.
	otel.SetTracerProvider(tp)

	// 2. Install the middleware once, on the client.
	client := anthropic.NewClient(option.WithMiddleware(traceaianthropic.Middleware()))

	// 3. Optional: group the run into a session.
	if session != "" {
		ctx = traceaianthropic.ContextWithSessionID(ctx, session)
	}

	answer, traceID, err := weatherAgent(ctx, client, model, question, stream)
	if err != nil {
		return err
	}
	fmt.Println("answer:", answer)
	fmt.Println("trace_id:", traceID)

	if fail {
		_, err := client.Messages.New(ctx, anthropic.MessageNewParams{
			Model:     "no-such-model-traceai-example",
			MaxTokens: 16,
			Messages:  []anthropic.MessageParam{anthropic.NewUserMessage(anthropic.NewTextBlock("hello"))},
		})
		var apiErr *anthropic.Error
		if errors.As(err, &apiErr) {
			fmt.Println("expected failure: HTTP", apiErr.StatusCode)
		} else {
			fmt.Println("expected failure, got:", err)
		}
	}
	return nil
}

func newTracerProvider(ctx context.Context) (*sdktrace.TracerProvider, error) {
	apiKey, secretKey, project := os.Getenv("FI_API_KEY"), os.Getenv("FI_SECRET_KEY"), os.Getenv("FI_PROJECT_NAME")
	if apiKey == "" || secretKey == "" || project == "" {
		return nil, errors.New("set FI_API_KEY, FI_SECRET_KEY and FI_PROJECT_NAME")
	}
	base := strings.TrimRight(os.Getenv("FI_BASE_URL"), "/")
	if base == "" {
		base = "https://api.futureagi.com"
	}
	exporter, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpointURL(base+"/tracer/v1/traces"),
		otlptracehttp.WithHeaders(map[string]string{"X-Api-Key": apiKey, "X-Secret-Key": secretKey}),
		otlptracehttp.WithCompression(otlptracehttp.NoCompression),
	)
	if err != nil {
		return nil, err
	}
	res := resource.NewSchemaless(
		attribute.String("project_name", project),
		attribute.String("project_type", "observe"),
		attribute.String("service.name", "anthropic-go-weather-agent"),
	)
	return sdktrace.NewTracerProvider(sdktrace.WithBatcher(exporter), sdktrace.WithResource(res)), nil
}

var weatherTool = anthropic.ToolParam{
	Name:        "get_weather",
	Description: anthropic.String("Current weather for a city."),
	InputSchema: anthropic.ToolInputSchemaParam{
		Properties: map[string]any{"city": map[string]any{"type": "string"}},
		Required:   []string{"city"},
	},
}

// weatherAgent asks the model, runs the tool the model asks for, and asks again
// with the tool's result. It returns the answer and the run's trace id.
func weatherAgent(ctx context.Context, client anthropic.Client, model, question string, stream bool) (string, string, error) {
	cfg := traceaianthropic.TraceConfigFromEnv()
	tracer := otel.Tracer("anthropic-go-weather-agent")
	ctx, agent := tracer.Start(ctx, "weather-agent", trace.WithAttributes(
		attribute.String("gen_ai.span.kind", "AGENT"),
		attribute.String("gen_ai.operation.name", "invoke_agent"),
		attribute.String("gen_ai.agent.name", "weather-agent"),
	))
	defer agent.End()
	if id, ok := traceaianthropic.SessionIDFromContext(ctx); ok {
		agent.SetAttributes(attribute.String("session.id", id))
	}
	if !cfg.HideInputs {
		agent.SetAttributes(attribute.String("input.value", question))
	}
	traceID := agent.SpanContext().TraceID().String()

	params := anthropic.MessageNewParams{
		Model:     anthropic.Model(model),
		MaxTokens: 512,
		System:    []anthropic.TextBlockParam{{Text: "You answer weather questions. Always call get_weather first."}},
		Messages:  []anthropic.MessageParam{anthropic.NewUserMessage(anthropic.NewTextBlock(question))},
		Tools:     []anthropic.ToolUnionParam{{OfTool: &weatherTool}},
	}
	first, err := client.Messages.New(ctx, params)
	if err != nil {
		agent.SetStatus(codes.Error, "first model call failed")
		return "", traceID, err
	}
	params.Messages = append(params.Messages, first.ToParam())
	var results []anthropic.ContentBlockParamUnion
	for _, block := range first.Content {
		if tu, ok := block.AsAny().(anthropic.ToolUseBlock); ok {
			out := runTool(ctx, tracer, cfg, tu)
			results = append(results, anthropic.NewToolResultBlock(tu.ID, out, false))
		}
	}
	if len(results) == 0 {
		answer := text(first.Content)
		setOutput(agent, cfg, answer)
		return answer, traceID, nil
	}
	params.Messages = append(params.Messages, anthropic.NewUserMessage(results...))

	var final anthropic.Message
	if stream {
		s := client.Messages.NewStreaming(ctx, params)
		for s.Next() {
			if err := final.Accumulate(s.Current()); err != nil {
				return "", traceID, err
			}
		}
		if err := s.Err(); err != nil {
			agent.SetStatus(codes.Error, "streamed model call failed")
			return "", traceID, err
		}
		s.Close()
	} else {
		m, err := client.Messages.New(ctx, params)
		if err != nil {
			agent.SetStatus(codes.Error, "second model call failed")
			return "", traceID, err
		}
		final = *m
	}
	answer := text(final.Content)
	setOutput(agent, cfg, answer)
	return answer, traceID, nil
}

// runTool runs the tool in a TOOL span. The middleware does not span your
// tools: their code runs in your process, not in the API call.
func runTool(ctx context.Context, tracer trace.Tracer, cfg traceaianthropic.TraceConfig, tu anthropic.ToolUseBlock) string {
	_, span := tracer.Start(ctx, "execute_tool "+tu.Name, trace.WithAttributes(
		attribute.String("gen_ai.span.kind", "TOOL"),
		attribute.String("gen_ai.operation.name", "execute_tool"),
		attribute.String("gen_ai.tool.name", tu.Name),
		attribute.String("gen_ai.tool.call.id", tu.ID),
	))
	defer span.End()
	var args struct {
		City string `json:"city"`
	}
	_ = json.Unmarshal(tu.Input, &args)
	out, _ := json.Marshal(map[string]any{"city": args.City, "condition": "sunny", "temperature_c": 21})
	if !cfg.HideInputs {
		span.SetAttributes(attribute.String("input.value", string(tu.Input)))
	}
	if !cfg.HideOutputs {
		span.SetAttributes(attribute.String("output.value", string(out)))
	}
	return string(out)
}

func setOutput(span trace.Span, cfg traceaianthropic.TraceConfig, answer string) {
	if !cfg.HideOutputs {
		span.SetAttributes(attribute.String("output.value", answer))
	}
}

func text(blocks []anthropic.ContentBlockUnion) string {
	var b strings.Builder
	for _, block := range blocks {
		if block.Type == "text" {
			b.WriteString(block.Text)
		}
	}
	return b.String()
}
