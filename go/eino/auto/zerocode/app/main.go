// Command app is an Eino agent program whose only tracing code is the
// blank import of this module. The tests build and run it against fake
// Future AGI and model endpoints.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/cloudwego/eino-ext/components/model/openai"
	"github.com/cloudwego/eino/adk"
	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/components/tool/utils"
	"github.com/cloudwego/eino/compose"
	"github.com/future-agi/traceAI/go/auto"
	_ "github.com/future-agi/traceAI/go/eino/auto"

	traceaieino "github.com/future-agi/traceAI/go/eino"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func main() {
	if err := run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

// APP_MODE selects what the application does besides asking two questions:
//
//	plain        nothing: the blank import is all there is
//	ownprovider  installs its own global SDK provider exporting to APP_COLLECTOR
//	manual       ownprovider, and registers traceaieino.NewHandler() globally
//	unregistered ownprovider, and builds a handler it never uses
//	perrun       ownprovider; the first question runs with its own handler
//	midrun       ownprovider; a tool registers a global handler during the first question
//	concurrent   plain; eight questions run at once
func run(ctx context.Context) error {
	mode := os.Getenv("APP_MODE")
	var manual []*traceaieino.Handler
	var tp *sdktrace.TracerProvider
	if mode != "plain" && mode != "concurrent" && mode != "" {
		exp, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(os.Getenv("APP_COLLECTOR")+"/v1/traces"))
		if err != nil {
			return err
		}
		tp = sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
		otel.SetTracerProvider(tp)
		defer tp.Shutdown(ctx)
	}
	var once sync.Once
	register := func() {
		once.Do(func() {
			h := traceaieino.NewHandler()
			manual = append(manual, h)
			callbacks.AppendGlobalHandlers(h)
		})
	}
	var runOpts []adk.AgentRunOption
	switch mode {
	case "manual":
		register()
	case "unregistered":
		_ = traceaieino.NewHandler()
	case "perrun":
		h := traceaieino.NewHandler(traceaieino.WithTracerProvider(tp))
		manual = append(manual, h)
		runOpts = append(runOpts, adk.WithCallbacks(h))
	}
	agent, err := newAgent(ctx, func() {
		if mode == "midrun" {
			register()
		}
	})
	if err != nil {
		return err
	}
	if mode == "concurrent" {
		var wg sync.WaitGroup
		errs := make(chan error, 8)
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() { defer wg.Done(); errs <- ask(ctx, agent) }()
		}
		wg.Wait()
		close(errs)
		for err := range errs {
			if err != nil {
				return err
			}
		}
	} else if err := ask(ctx, agent, runOpts...); err != nil { // question A
		return err
	}
	if mode == "perrun" || mode == "midrun" {
		if err := ask(ctx, agent); err != nil { // question B: no per-run handler
			return err
		}
	}
	report, _ := json.Marshal(auto.Report().Integrations)
	fmt.Println("REPORT " + string(report))
	// Go has no exit hook: flush what the agent queued.
	sctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	for _, h := range manual {
		_ = h.Shutdown(sctx) // finishes an agent's span, in the background
	}
	return auto.Shutdown(sctx)
}

func newAgent(ctx context.Context, inTool func()) (adk.Agent, error) {
	chat, err := openai.NewChatModel(ctx, &openai.ChatModelConfig{APIKey: "sk-model-key-0003", BaseURL: os.Getenv("OPENAI_BASE_URL"), Model: "gpt-test-1", Timeout: time.Minute})
	if err != nil {
		return nil, err
	}
	weather, err := utils.InferTool("get_weather", "Weather for a city.", func(_ context.Context, r *struct {
		City string `json:"city"`
	}) (string, error) {
		inTool()
		if strings.TrimSpace(r.City) == "" {
			return "", errors.New("city is required")
		}
		return "light rain", nil
	})
	if err != nil {
		return nil, err
	}
	return adk.NewChatModelAgent(ctx, &adk.ChatModelAgentConfig{
		Name: "trip_assistant", Description: "Plans what to pack.", Instruction: "Call get_weather first.",
		Model:       chat,
		ToolsConfig: adk.ToolsConfig{ToolsNodeConfig: compose.ToolsNodeConfig{Tools: []tool.BaseTool{weather}}},
	})
}

func ask(ctx context.Context, agent adk.Agent, opts ...adk.AgentRunOption) error {
	events := adk.NewRunner(ctx, adk.RunnerConfig{Agent: agent}).Query(ctx, os.Getenv("APP_QUESTION"), opts...)
	for {
		ev, ok := events.Next()
		if !ok {
			return nil
		}
		if ev.Err != nil {
			return ev.Err
		}
	}
}
