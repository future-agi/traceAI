// Command example runs a small weather agent on the Google Gen AI Go SDK
// (google.golang.org/genai, Gemini Developer API) and traces it to Future AGI
// with traceaigenai.
//
//	export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service GEMINI_API_KEY=...
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

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/genai"
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
	model := flag.String("model", envOr("GEMINI_MODEL", "gemini-3.8-flash"), "Gemini model")
	question := flag.String("question", "What is the weather in Paris right now? Answer in one sentence.", "user question")
	stream := flag.Bool("stream", false, "stream the final answer")
	fail := flag.Bool("fail", false, "also call a model that does not exist, to show an ERROR span")
	session := flag.String("session", fmt.Sprintf("gemini-go-example-%d", time.Now().Unix()), "session id")
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

	// 3. The Gemini client with the traced HTTP client. The SDK reads
	//    GEMINI_API_KEY (or GOOGLE_API_KEY) as usual.
	client, err := genai.NewClient(ctx, &genai.ClientConfig{
		Backend:    genai.BackendGeminiAPI,
		HTTPClient: traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)),
	})
	if err != nil {
		return err
	}

	// Spans this program starts itself obey the same FI_HIDE_* switches.
	tc := traceaigenai.TraceConfigFromEnv()
	tracer := tp.Tracer("gemini-go-example")

	sessionCtx := traceaigenai.ContextWithSessionID(ctx, *session)
	ctx, agent := tracer.Start(sessionCtx, "weather-agent", trace.WithAttributes(
		attribute.String(traceaigenai.GenAISpanKind, traceaigenai.SpanKindAgent),
		attribute.String(traceaigenai.GenAIOperationName, "invoke_agent"),
		attribute.String("gen_ai.agent.name", "weather-agent"),
	), trace.WithAttributes(traceaigenai.ContextAttributes(sessionCtx)...))
	tc.SetAttributes(agent, attribute.String(traceaigenai.InputValue, *question))
	fmt.Printf("trace_id=%s session=%s\n", agent.SpanContext().TraceID(), *session)

	answer, err := askWithTool(ctx, client, tracer, tc, *model, *question, *toolNote, *stream)
	if err != nil {
		agent.SetStatus(codes.Error, "agent failed")
		agent.End()
		return err
	}
	tc.SetAttributes(agent, attribute.String(traceaigenai.OutputValue, answer))
	agent.End()
	fmt.Println("answer:", answer)

	if *fail {
		// A separate trace in the same session.
		_, err := client.Models.GenerateContent(sessionCtx, "no-such-model-th8273", genai.Text(*question), nil)
		var apiErr genai.APIError
		if errors.As(err, &apiErr) {
			fmt.Printf("expected failure: HTTP %d %s\n", apiErr.Code, apiErr.Status)
		} else {
			fmt.Printf("expected failure, got: %v\n", err)
		}
	}
	return nil
}

func askWithTool(ctx context.Context, client *genai.Client, tracer trace.Tracer, tc traceaigenai.TraceConfig,
	model, question, toolNote string, stream bool) (string, error) {
	config := &genai.GenerateContentConfig{
		SystemInstruction: genai.NewContentFromText("You are a weather assistant. Always call get_weather before answering.", genai.RoleUser),
		Tools: []*genai.Tool{{FunctionDeclarations: []*genai.FunctionDeclaration{{
			Name:        "get_weather",
			Description: "Current weather for a city",
			Parameters: &genai.Schema{
				Type:       genai.TypeObject,
				Properties: map[string]*genai.Schema{"city": {Type: genai.TypeString}},
				Required:   []string{"city"},
			},
		}}}},
	}
	history := genai.Text(question)

	first, err := client.Models.GenerateContent(ctx, model, history, config)
	if err != nil {
		return "", err
	}
	calls := first.FunctionCalls()
	if len(calls) == 0 {
		return first.Text(), nil
	}
	// Send the model's turn back unchanged (it carries the thought signature
	// Gemini needs), then one function response per call.
	history = append(history, first.Candidates[0].Content)
	var results []*genai.Part
	for _, call := range calls {
		result := runTool(ctx, tracer, tc, call.ID, call.Name, call.Args, toolNote)
		part := genai.NewPartFromFunctionResponse(call.Name, result)
		part.FunctionResponse.ID = call.ID
		results = append(results, part)
	}
	history = append(history, genai.NewContentFromParts(results, genai.RoleUser))

	if !stream {
		final, err := client.Models.GenerateContent(ctx, model, history, config)
		if err != nil {
			return "", err
		}
		return final.Text(), nil
	}

	var b strings.Builder
	for chunk, err := range client.Models.GenerateContentStream(ctx, model, history, config) {
		if err != nil {
			return b.String(), err
		}
		b.WriteString(chunk.Text())
	}
	return b.String(), nil
}

// runTool executes the tool locally inside its own span. The transport spans
// the model calls; your tool code is yours to span.
func runTool(ctx context.Context, tracer trace.Tracer, tc traceaigenai.TraceConfig, id, name string, args map[string]any, note string) map[string]any {
	_, span := tracer.Start(ctx, "execute_tool "+name, trace.WithAttributes(
		attribute.String(traceaigenai.GenAISpanKind, traceaigenai.SpanKindTool),
		attribute.String(traceaigenai.GenAIOperationName, "execute_tool"),
		attribute.String(traceaigenai.GenAIToolName, name),
		attribute.String(traceaigenai.GenAIToolCallID, id),
	), trace.WithAttributes(traceaigenai.ContextAttributes(ctx)...))
	defer span.End()
	in, _ := json.Marshal(args)
	tc.SetAttributes(span,
		attribute.String(traceaigenai.InputValue, string(in)),
		attribute.String(traceaigenai.InputMimeType, traceaigenai.MimeTypeJSON))

	city, _ := args["city"].(string)
	out := map[string]any{"city": city, "temperature_c": 21, "conditions": "sunny"}
	if note != "" {
		out["note"] = note
	}
	b, _ := json.Marshal(out)
	tc.SetAttributes(span,
		attribute.String(traceaigenai.OutputValue, string(b)),
		attribute.String(traceaigenai.OutputMimeType, traceaigenai.MimeTypeJSON))
	return out
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
