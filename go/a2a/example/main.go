// Command example sends A2A messages with the official Go SDK
// (github.com/a2aproject/a2a-go/v2) and traces the client calls to Future AGI
// with traceaia2a.
//
//	export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service
//	go run ./example -stream -fail
//
// With -agent-url it talks to your agent (the URL its agent card is served
// from). Without it, it starts a small demo agent on 127.0.0.1, built with
// a2a-go's own server package, that answers with a model when OPENAI_API_KEY
// is set (any OpenAI-compatible endpoint through OPENAI_BASE_URL) and echoes
// otherwise.
//
// One run is one trace: your agent span, one span per A2A call from the
// interceptor, and, for the demo agent, the agent's own spans, which join the
// trace because the interceptor sends traceparent. -fail adds a call the demo
// agent answers with a failed task, to show an ERROR span.
//
// The exporter is the stock OpenTelemetry OTLP/HTTP exporter: Future AGI needs
// the X-Api-Key and X-Secret-Key headers, the project_name resource attribute,
// and an uncompressed body.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"iter"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"github.com/a2aproject/a2a-go/v2/a2aclient/agentcard"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	traceaia2a "github.com/future-agi/traceAI/go/a2a"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

var (
	agentURL = flag.String("agent-url", "", "base URL of the A2A agent (where its agent card is served); empty starts the demo agent")
	question = flag.String("question", "Plan a one-day trip to Kyoto in three bullet points.", "message to send")
	session  = flag.String("session", "", "session id (default: the A2A context id)")
	stream   = flag.Bool("stream", false, "use SendStreamingMessage")
	fail     = flag.Bool("fail", false, "also send a message the demo agent answers with a failed task")
	model    = flag.String("model", envOr("MODEL", "gpt-4.1-mini"), "model the demo agent calls when OPENAI_API_KEY is set")
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

// shutdown flushes every queued span. A span that is only queued when the
// process exits is lost, so call it on every exit path, SIGTERM included.
func shutdown(tp *sdktrace.TracerProvider) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
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
	defer shutdown(tp) // flush before exit

	url := *agentURL
	if url == "" {
		demo, stop, err := startDemoAgent(tp)
		if err != nil {
			return err
		}
		defer stop()
		url = demo
	}

	card, err := agentcard.DefaultResolver.Resolve(ctx, url)
	if err != nil {
		return fmt.Errorf("resolve agent card: %w", err)
	}
	client, err := a2aclient.NewFromCard(ctx, card,
		a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(traceaia2a.WithTracerProvider(tp))))
	if err != nil {
		return err
	}
	defer func() { _ = client.Destroy() }()

	sessionCtx := traceaia2a.ContextWithSessionID(ctx, *session)
	tracer := tp.Tracer("a2a-go-example")
	ctx, root := tracer.Start(sessionCtx, "trip-planner", trace.WithAttributes(
		attribute.String(traceaia2a.GenAISpanKind, traceaia2a.SpanKindAgent),
		attribute.String(traceaia2a.GenAIOperationName, "invoke_agent"),
		attribute.String("gen_ai.agent.name", "trip-planner"),
	), trace.WithAttributes(traceaia2a.ContextAttributes(sessionCtx)...))
	defer root.End()
	// TraceConfig applies the same FI_HIDE_* switches to values you add yourself.
	traceaia2a.TraceConfigFromEnv().SetAttributes(root, attribute.String(traceaia2a.InputValue, *question))
	fmt.Println("trace_id=" + root.SpanContext().TraceID().String())

	req := &a2a.SendMessageRequest{Message: a2a.NewMessage(a2a.MessageRoleUser, a2a.NewTextPart(*question))}
	if err := send(ctx, client, req); err != nil {
		root.SetStatus(codes.Error, "a2a call failed")
		return err
	}

	if *fail {
		failing := &a2a.SendMessageRequest{
			Message:  a2a.NewMessage(a2a.MessageRoleUser, a2a.NewTextPart(*question)),
			Metadata: map[string]any{"demo.fail": true},
		}
		if err := send(ctx, client, failing); err != nil {
			return err
		}
	}
	return nil
}

// send makes one A2A call, streamed or not, and prints what came back.
func send(ctx context.Context, client *a2aclient.Client, req *a2a.SendMessageRequest) error {
	if *stream {
		for ev, err := range client.SendStreamingMessage(ctx, req) {
			if err != nil {
				return err
			}
			printEvent(ev)
		}
		return nil
	}
	res, err := client.SendMessage(ctx, req)
	if err != nil {
		return err
	}
	printEvent(res)
	return nil
}

func printEvent(ev a2a.Event) {
	switch v := ev.(type) {
	case *a2a.Task:
		fmt.Printf("task %s %s\n", v.ID, v.Status.State)
	case *a2a.TaskStatusUpdateEvent:
		fmt.Printf("status %s\n", v.Status.State)
	case *a2a.TaskArtifactUpdateEvent:
		fmt.Println("artifact")
	case *a2a.Message:
		fmt.Println("message")
	}
}

// startDemoAgent serves a small A2A agent on 127.0.0.1 with a2a-go's own
// server package and returns the URL its agent card is served from.
//
// The agent stands in for a remote agent. traceaia2a does not instrument
// a2asrv: the agent's spans below come from its own code, which reads the
// traceparent the interceptor sent so they join the caller's trace. A remote
// agent that does the same (or uses any OpenTelemetry HTTP middleware) shows up
// in the same trace; one that does not still leaves the client span.
func startDemoAgent(tp *sdktrace.TracerProvider) (string, func(), error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", nil, err
	}
	base := "http://" + ln.Addr().String()
	card := &a2a.AgentCard{
		Name:                "trip-helper",
		Description:         "Demo A2A agent for the traceAI example",
		Version:             "1.0.0",
		SupportedInterfaces: []*a2a.AgentInterface{a2a.NewAgentInterface(base+"/a2a", a2a.TransportProtocolJSONRPC)},
		DefaultInputModes:   []string{"text/plain"},
		DefaultOutputModes:  []string{"text/plain"},
		Capabilities:        a2a.AgentCapabilities{Streaming: true},
		Skills:              []a2a.AgentSkill{{ID: "plan", Name: "Plan", Description: "Answers travel questions.", Tags: []string{"travel"}}},
	}
	agent := &demoAgent{tracer: tp.Tracer("demo-remote-agent"), tc: traceaia2a.TraceConfigFromEnv()}
	mux := http.NewServeMux()
	mux.Handle("/a2a", a2asrv.NewJSONRPCHandler(a2asrv.NewHandler(agent)))
	mux.Handle(a2asrv.WellKnownAgentCardPath, a2asrv.NewStaticAgentCardHandler(card))
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	stop := func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}
	return base, stop, nil
}

type demoAgent struct {
	tracer trace.Tracer
	tc     traceaia2a.TraceConfig
}

// Execute answers one message: submitted, working, one artifact, completed
// (or failed when the request carries demo.fail).
func (d *demoAgent) Execute(ctx context.Context, ec *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		question := ""
		if ec.Message != nil {
			for _, p := range ec.Message.Parts {
				question += p.Text()
			}
		}
		// Continue the caller's trace from the traceparent it sent.
		carrier := propagation.MapCarrier{}
		if v, ok := ec.ServiceParams.Get("traceparent"); ok && len(v) > 0 {
			carrier.Set("traceparent", v[0])
		}
		ctx = propagation.TraceContext{}.Extract(ctx, carrier)
		ctx, span := d.tracer.Start(ctx, "trip-helper", trace.WithSpanKind(trace.SpanKindServer),
			trace.WithAttributes(
				attribute.String(traceaia2a.GenAISpanKind, traceaia2a.SpanKindAgent),
				attribute.String("gen_ai.agent.name", "trip-helper"),
				attribute.String(traceaia2a.A2ATaskID, string(ec.TaskID)),
			))
		defer span.End()
		d.tc.SetAttributes(span, attribute.String(traceaia2a.InputValue, question))

		if !yield(a2a.NewSubmittedTask(ec, ec.Message), nil) ||
			!yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateWorking, nil), nil) {
			return
		}
		if fail, _ := ec.Metadata["demo.fail"].(bool); fail {
			span.SetStatus(codes.Error, "demo failure")
			yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateFailed, a2a.NewMessageForTask(a2a.MessageRoleAgent, ec,
				a2a.NewTextPart("could not plan: "+question))), nil)
			return
		}
		answer, err := d.answer(ctx, question)
		if err != nil {
			span.SetStatus(codes.Error, "model call failed")
			yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateFailed, a2a.NewMessageForTask(a2a.MessageRoleAgent, ec,
				a2a.NewTextPart("model call failed"))), nil)
			return
		}
		d.tc.SetAttributes(span, attribute.String(traceaia2a.OutputValue, answer))
		_ = yield(a2a.NewArtifactEvent(ec, a2a.NewTextPart(answer)), nil) &&
			yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCompleted, a2a.NewMessageForTask(a2a.MessageRoleAgent, ec,
				a2a.NewTextPart("done"))), nil)
	}
}

// Cancel marks the task canceled.
func (d *demoAgent) Cancel(ctx context.Context, ec *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCanceled, nil), nil)
	}
}

// answer calls an OpenAI-compatible chat completions endpoint when
// OPENAI_API_KEY is set, inside an LLM span of the demo agent, and echoes
// otherwise (no model call, so no LLM span).
func (d *demoAgent) answer(ctx context.Context, question string) (string, error) {
	key := os.Getenv("OPENAI_API_KEY")
	if key == "" {
		return "echo: " + question, nil
	}
	_, span := d.tracer.Start(ctx, "chat "+*model, trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String(traceaia2a.GenAISpanKind, "LLM"),
			attribute.String(traceaia2a.GenAIOperationName, "chat"),
			attribute.String("gen_ai.request.model", *model),
		))
	defer span.End()
	d.tc.SetAttributes(span, attribute.String(traceaia2a.InputValue, question))

	body, _ := json.Marshal(map[string]any{
		"model": *model,
		"messages": []map[string]string{
			{"role": "system", "content": "You are a concise travel planner."},
			{"role": "user", "content": question},
		},
	})
	base := strings.TrimRight(envOr("OPENAI_BASE_URL", "https://api.openai.com/v1"), "/")
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	httpReq.Header.Set("Authorization", "Bearer "+key)
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(httpReq)
	if err != nil {
		span.SetAttributes(attribute.String(traceaia2a.ErrorType, "transport"))
		span.SetStatus(codes.Error, "transport")
		return "", err
	}
	defer resp.Body.Close()
	var out struct {
		Model   string `json:"model"`
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
		Usage *struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		} `json:"usage"`
	}
	if resp.StatusCode/100 != 2 || json.NewDecoder(resp.Body).Decode(&out) != nil || len(out.Choices) == 0 {
		span.SetAttributes(attribute.Int("http.response.status_code", resp.StatusCode))
		span.SetStatus(codes.Error, "bad model response")
		return "", fmt.Errorf("model call: HTTP %d", resp.StatusCode)
	}
	if out.Model != "" {
		span.SetAttributes(attribute.String("gen_ai.response.model", out.Model))
	}
	if u := out.Usage; u != nil {
		span.SetAttributes(
			attribute.Int("gen_ai.usage.input_tokens", u.PromptTokens),
			attribute.Int("gen_ai.usage.output_tokens", u.CompletionTokens),
			attribute.Int("gen_ai.usage.total_tokens", u.TotalTokens),
		)
	}
	answer := out.Choices[0].Message.Content
	d.tc.SetAttributes(span, attribute.String(traceaia2a.OutputValue, answer))
	return answer, nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
