// Command example runs a CloudWeGo Eino agent with a tool and traces it to
// Future AGI with traceaieino.
//
//	export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service
//	export OPENAI_API_KEY=... MODEL=gpt-4.1-mini
//	go run ./example -stream -fail
//
// The agent is an Eino ADK ChatModelAgent with one tool (get_weather), run
// through adk.Runner. Its chat model is the eino-ext OpenAI component, so any
// OpenAI-compatible endpoint works through OPENAI_BASE_URL. One question is
// one trace: a request span the program starts, the agent span, the agent's
// graph and nodes, two model calls and the tool call. -stream streams the
// model's reply; -fail also asks an agent whose model does not exist, to show
// an ERROR trace.
//
// The handler is registered once, with callbacks.AppendGlobalHandlers,
// before any agent runs. The exporter is the stock OpenTelemetry OTLP/HTTP
// exporter: Future AGI needs the X-Api-Key and X-Secret-Key headers, the
// project_name resource attribute, and an uncompressed body.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/cloudwego/eino-ext/components/model/openai"
	"github.com/cloudwego/eino/adk"
	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/components/tool/utils"
	"github.com/cloudwego/eino/compose"
	traceaieino "github.com/future-agi/traceAI/go/eino"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

var (
	question = flag.String("question", "I am in Kyoto tomorrow. What should I pack?", "question for the agent")
	session  = flag.String("session", "", "session id (default: one per run)")
	stream   = flag.Bool("stream", false, "stream the model's reply")
	fail     = flag.Bool("fail", false, "also ask an agent whose model does not exist")
	model    = flag.String("model", envOr("MODEL", "gpt-4.1-mini"), "chat model")
)

func main() {
	flag.Parse()
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

// shutdown stops the handler, then flushes and closes the provider. A span
// that is only queued when the process exits is lost, so call it on every
// exit path, SIGTERM included.
func shutdown(h *traceaieino.Handler, tp *sdktrace.TracerProvider) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.Shutdown(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "trace handler:", err)
	}
	if err := tp.Shutdown(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "trace export:", err)
	}
}

func run() error {
	ctx := context.Background()
	tp, err := newTracerProvider(ctx)
	if err != nil {
		return err
	}
	h := traceaieino.NewHandler(traceaieino.WithTracerProvider(tp))
	callbacks.AppendGlobalHandlers(h) // once, before any graph or agent runs
	defer shutdown(h, tp)             // flush before exit

	sessionID := *session
	if sessionID == "" {
		sessionID = fmt.Sprintf("eino-example-%d", time.Now().UnixNano())
	}
	ctx = traceaieino.ContextWithSessionID(ctx, sessionID)

	agent, err := newAgent(ctx, *model)
	if err != nil {
		return err
	}
	answer, err := ask(ctx, tp, agent, *question)
	if err != nil {
		return err
	}
	fmt.Println("answer:", answer)

	if *fail {
		broken, err := newAgent(ctx, "no-such-model")
		if err != nil {
			return err
		}
		if _, err := ask(ctx, tp, broken, *question); err == nil {
			return errors.New("the failing run did not fail")
		}
		fmt.Println("failing run: model call failed as expected")
	}
	return nil
}

// newAgent builds a ChatModelAgent: the eino-ext OpenAI chat model (any
// OpenAI-compatible endpoint through OPENAI_BASE_URL) and the get_weather
// tool.
func newAgent(ctx context.Context, modelName string) (adk.Agent, error) {
	chat, err := openai.NewChatModel(ctx, &openai.ChatModelConfig{
		APIKey:  os.Getenv("OPENAI_API_KEY"),
		BaseURL: os.Getenv("OPENAI_BASE_URL"), // empty: api.openai.com
		Model:   modelName,
		Timeout: 2 * time.Minute,
	})
	if err != nil {
		return nil, err
	}
	weather, err := utils.InferTool("get_weather", "Get tomorrow's weather forecast for a city.", getWeather)
	if err != nil {
		return nil, err
	}
	return adk.NewChatModelAgent(ctx, &adk.ChatModelAgentConfig{
		Name:        "trip_assistant",
		Description: "Plans what to pack for a trip.",
		Instruction: "Call get_weather for the city first, then answer in two short sentences.",
		Model:       chat,
		ToolsConfig: adk.ToolsConfig{ToolsNodeConfig: compose.ToolsNodeConfig{
			Tools: []tool.BaseTool{weather},
		}},
	})
}

type weatherRequest struct {
	City string `json:"city" jsonschema:"description=City name, for example Kyoto"`
}

type forecast struct {
	City     string `json:"city"`
	Sky      string `json:"sky"`
	HighC    int    `json:"high_c"`
	LowC     int    `json:"low_c"`
	RainProb string `json:"rain_probability"`
}

// getWeather is a local stand-in for a weather API, so the example needs no
// second key.
func getWeather(_ context.Context, req *weatherRequest) (*forecast, error) {
	if strings.TrimSpace(req.City) == "" {
		return nil, errors.New("city is required")
	}
	return &forecast{City: req.City, Sky: "light rain in the afternoon", HighC: 19, LowC: 12, RainProb: "70%"}, nil
}

// ask runs one question through the agent under a request span, prints the
// trace id, and returns the agent's last message.
func ask(ctx context.Context, tp trace.TracerProvider, agent adk.Agent, q string) (string, error) {
	ctx, span := tp.Tracer("eino-example").Start(ctx, "trip_request",
		trace.WithAttributes(append(traceaieino.ContextAttributes(ctx),
			attribute.String(traceaieino.GenAISpanKind, traceaieino.SpanKindChain))...))
	defer span.End()
	fmt.Println("trace_id=" + span.SpanContext().TraceID().String())
	// The question and answer on your own span obey the same FI_HIDE_* switches.
	tc := traceaieino.TraceConfigFromEnv()
	tc.SetAttributes(span, attribute.String(traceaieino.InputValue, q))

	runner := adk.NewRunner(ctx, adk.RunnerConfig{Agent: agent, EnableStreaming: *stream})
	events := runner.Query(ctx, q)
	var answer string
	for {
		ev, ok := events.Next()
		if !ok {
			break
		}
		if ev.Err != nil {
			span.SetStatus(codes.Error, "agent failed")
			return "", ev.Err
		}
		if ev.Output == nil || ev.Output.MessageOutput == nil {
			continue
		}
		mo := ev.Output.MessageOutput
		if mo.IsStreaming {
			text, err := readStream(mo)
			if err != nil {
				span.SetStatus(codes.Error, "stream failed")
				return "", err
			}
			answer = text
			continue
		}
		if mo.Message != nil {
			answer = mo.Message.Content
		}
	}
	tc.SetAttributes(span, attribute.String(traceaieino.OutputValue, answer))
	span.SetStatus(codes.Ok, "")
	return answer, nil
}

func readStream(mo *adk.MessageVariant) (string, error) {
	defer mo.MessageStream.Close()
	var b strings.Builder
	for {
		chunk, err := mo.MessageStream.Recv()
		if errors.Is(err, io.EOF) {
			return b.String(), nil
		}
		if err != nil {
			return "", err
		}
		b.WriteString(chunk.Content)
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
