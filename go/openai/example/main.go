// Command example runs a small weather agent on the official OpenAI Go SDK and
// traces it to Future AGI with traceaiopenai.
//
//	export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service OPENAI_API_KEY=...
//	go run ./example -stream
//
// One run is one trace: an agent span, an LLM span that asks for a tool, the
// tool span, and an LLM span for the final answer. -fail adds one call to a
// model that does not exist, to show an ERROR span.
//
// The exporter is the stock OpenTelemetry OTLP/HTTP exporter: Future AGI needs
// the X-Api-Key and X-Secret-Key headers, the project_name resource attribute,
// and an uncompressed body.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	traceaiopenai "github.com/future-agi/traceAI/go/openai"
	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	"github.com/openai/openai-go/v3/shared"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

// newTracerProvider sends spans to Future AGI over OTLP/HTTP: the full traces
// URL, both auth headers, no gzip (the collector rejects it), and the
// project_name / project_type resource attributes. It adds no host, process or
// OS resource detector, so no host name, user name or local path is exported.
func newTracerProvider(ctx context.Context) (*sdktrace.TracerProvider, error) {
	apiKey, secretKey := os.Getenv("FI_API_KEY"), os.Getenv("FI_SECRET_KEY")
	project := os.Getenv("FI_PROJECT_NAME")
	if apiKey == "" || secretKey == "" || project == "" {
		return nil, errors.New("set FI_API_KEY, FI_SECRET_KEY and FI_PROJECT_NAME")
	}
	base := strings.TrimRight(envOr("FI_BASE_URL", "https://api.futureagi.com"), "/")

	exp, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpointURL(base+"/tracer/v1/traces"),
		otlptracehttp.WithHeaders(map[string]string{
			"X-Api-Key":    apiKey,
			"X-Secret-Key": secretKey,
		}),
		otlptracehttp.WithCompression(otlptracehttp.NoCompression),
	)
	if err != nil {
		return nil, err
	}
	res, err := resource.Merge(resource.Default(), resource.NewSchemaless(
		attribute.String("project_name", project),
		attribute.String("project_type", "observe"),
		attribute.String("service.name", project),
	))
	if err != nil {
		return nil, err
	}
	return sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithBatcher(exp),
	), nil
}

// shutdown flushes every queued span. A span that is only queued when the
// process exits is lost.
func shutdown(tp *sdktrace.TracerProvider) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := tp.Shutdown(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "trace export:", err)
	}
}

func run() error {
	model := flag.String("model", envOr("OPENAI_MODEL", "gpt-4.1-mini"), "chat model")
	question := flag.String("question", "What is the weather in Paris right now? Answer in one sentence.", "user question")
	stream := flag.Bool("stream", false, "stream the final answer")
	fail := flag.Bool("fail", false, "also call a model that does not exist, to show an ERROR span")
	session := flag.String("session", fmt.Sprintf("openai-go-example-%d", time.Now().Unix()), "session id")
	toolNote := flag.String("tool-note", "", "extra text the tool returns (for privacy checks)")
	flag.Parse()

	ctx := context.Background()

	// 1. Exporter and provider.
	tp, err := newTracerProvider(ctx)
	if err != nil {
		return err
	}
	// 2. Flush before exit.
	defer shutdown(tp)

	// 3. The OpenAI client with the tracing middleware. OPENAI_API_KEY and
	//    OPENAI_BASE_URL are read by the SDK as usual.
	client := openai.NewClient(
		option.WithMiddleware(traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp))),
	)

	// Spans this program starts itself obey the same FI_HIDE_* switches.
	tc := traceaiopenai.TraceConfigFromEnv()
	tracer := tp.Tracer("openai-go-example")

	sessionCtx := traceaiopenai.ContextWithSessionID(ctx, *session)
	ctx, agent := tracer.Start(sessionCtx, "weather-agent", trace.WithAttributes(
		attribute.String(traceaiopenai.GenAISpanKind, traceaiopenai.SpanKindAgent),
		attribute.String(traceaiopenai.GenAIOperationName, "invoke_agent"),
		attribute.String("gen_ai.agent.name", "weather-agent"),
	), trace.WithAttributes(traceaiopenai.ContextAttributes(sessionCtx)...))
	tc.SetAttributes(agent, attribute.String(traceaiopenai.InputValue, *question))
	fmt.Printf("trace_id=%s session=%s\n", agent.SpanContext().TraceID(), *session)

	answer, err := askWithTool(ctx, client, tracer, tc, *model, *question, *toolNote, *stream)
	if err != nil {
		agent.SetStatus(codes.Error, "agent failed")
		agent.End()
		return err
	}
	tc.SetAttributes(agent, attribute.String(traceaiopenai.OutputValue, answer))
	agent.End()
	fmt.Println("answer:", answer)

	if *fail {
		// A separate trace in the same session.
		_, err := client.Chat.Completions.New(sessionCtx, openai.ChatCompletionNewParams{
			Model:    "no-such-model-th8271",
			Messages: []openai.ChatCompletionMessageParamUnion{openai.UserMessage(*question)},
		}, option.WithMaxRetries(0))
		var apiErr *openai.Error
		if errors.As(err, &apiErr) {
			fmt.Printf("expected failure: HTTP %d\n", apiErr.StatusCode)
		} else {
			fmt.Printf("expected failure, got: %v\n", err)
		}
	}
	return nil
}

func askWithTool(ctx context.Context, client openai.Client, tracer trace.Tracer, tc traceaiopenai.TraceConfig,
	model, question, toolNote string, stream bool) (string, error) {
	params := openai.ChatCompletionNewParams{
		Model: model,
		Messages: []openai.ChatCompletionMessageParamUnion{
			openai.SystemMessage("You are a weather assistant. Always call get_weather before answering."),
			openai.UserMessage(question),
		},
		Tools: []openai.ChatCompletionToolUnionParam{
			openai.ChatCompletionFunctionTool(shared.FunctionDefinitionParam{
				Name:        "get_weather",
				Description: openai.String("Current weather for a city"),
				Parameters: shared.FunctionParameters{
					"type":       "object",
					"properties": map[string]any{"city": map[string]any{"type": "string"}},
					"required":   []string{"city"},
				},
			}),
		},
	}

	first, err := client.Chat.Completions.New(ctx, params)
	if err != nil {
		return "", err
	}
	if len(first.Choices) == 0 {
		return "", errors.New("no choices in the response")
	}
	msg := first.Choices[0].Message
	if len(msg.ToolCalls) == 0 {
		return msg.Content, nil
	}
	params.Messages = append(params.Messages, msg.ToParam())
	for _, call := range msg.ToolCalls {
		result := runTool(ctx, tracer, tc, call.ID, call.Function.Name, call.Function.Arguments, toolNote)
		params.Messages = append(params.Messages, openai.ToolMessage(result, call.ID))
	}
	params.Tools = nil

	if !stream {
		final, err := client.Chat.Completions.New(ctx, params)
		if err != nil {
			return "", err
		}
		if len(final.Choices) == 0 {
			return "", errors.New("no choices in the final response")
		}
		return final.Choices[0].Message.Content, nil
	}

	// Ask for the usage chunk so the streamed span carries token counts.
	params.StreamOptions = openai.ChatCompletionStreamOptionsParam{IncludeUsage: openai.Bool(true)}
	s := client.Chat.Completions.NewStreaming(ctx, params)
	defer s.Close()
	var b strings.Builder
	for s.Next() {
		for _, c := range s.Current().Choices {
			b.WriteString(c.Delta.Content)
		}
	}
	return b.String(), s.Err()
}

// runTool executes the tool locally inside its own span. The middleware spans
// the model calls; your tool code is yours to span.
func runTool(ctx context.Context, tracer trace.Tracer, tc traceaiopenai.TraceConfig, id, name, args, note string) string {
	_, span := tracer.Start(ctx, "execute_tool "+name, trace.WithAttributes(
		attribute.String(traceaiopenai.GenAISpanKind, traceaiopenai.SpanKindTool),
		attribute.String(traceaiopenai.GenAIOperationName, "execute_tool"),
		attribute.String(traceaiopenai.GenAIToolName, name),
		attribute.String(traceaiopenai.GenAIToolCallID, id),
	), trace.WithAttributes(traceaiopenai.ContextAttributes(ctx)...))
	defer span.End()
	tc.SetAttributes(span,
		attribute.String(traceaiopenai.InputValue, args),
		attribute.String(traceaiopenai.InputMimeType, traceaiopenai.MimeTypeJSON))

	var in struct {
		City string `json:"city"`
	}
	_ = json.Unmarshal([]byte(args), &in)
	out := map[string]any{"city": in.City, "temperature_c": 21, "conditions": "sunny"}
	if note != "" {
		out["note"] = note
	}
	b, _ := json.Marshal(out)
	tc.SetAttributes(span,
		attribute.String(traceaiopenai.OutputValue, string(b)),
		attribute.String(traceaiopenai.OutputMimeType, traceaiopenai.MimeTypeJSON))
	return string(b)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
