// Command zerocodeapp is an Eino agent program whose only tracing code is the
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

// APP_MODE: "plain" does nothing else; "ownprovider" installs the
// application's own global SDK provider exporting to APP_COLLECTOR; "manual"
// does that and also registers traceaieino.NewHandler() itself.
func run(ctx context.Context) error {
	mode := os.Getenv("APP_MODE")
	if mode == "ownprovider" || mode == "manual" {
		exp, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(os.Getenv("APP_COLLECTOR")+"/v1/traces"))
		if err != nil {
			return err
		}
		tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
		otel.SetTracerProvider(tp)
		defer tp.Shutdown(ctx)
	}
	if mode == "manual" {
		callbacks.AppendGlobalHandlers(traceaieino.NewHandler())
	}
	chat, err := openai.NewChatModel(ctx, &openai.ChatModelConfig{APIKey: "sk-model-key-0003", BaseURL: os.Getenv("OPENAI_BASE_URL"), Model: "gpt-test-1", Timeout: time.Minute})
	if err != nil {
		return err
	}
	weather, err := utils.InferTool("get_weather", "Weather for a city.", func(_ context.Context, r *struct {
		City string `json:"city"`
	}) (string, error) {
		if strings.TrimSpace(r.City) == "" {
			return "", errors.New("city is required")
		}
		return "light rain", nil
	})
	if err != nil {
		return err
	}
	agent, err := adk.NewChatModelAgent(ctx, &adk.ChatModelAgentConfig{
		Name: "trip_assistant", Description: "Plans what to pack.", Instruction: "Call get_weather first.",
		Model:       chat,
		ToolsConfig: adk.ToolsConfig{ToolsNodeConfig: compose.ToolsNodeConfig{Tools: []tool.BaseTool{weather}}},
	})
	if err != nil {
		return err
	}
	events := adk.NewRunner(ctx, adk.RunnerConfig{Agent: agent}).Query(ctx, os.Getenv("APP_QUESTION"))
	for {
		ev, ok := events.Next()
		if !ok {
			break
		}
		if ev.Err != nil {
			return ev.Err
		}
	}
	report, _ := json.Marshal(auto.Report().Integrations)
	fmt.Println("REPORT " + string(report))
	// Go has no exit hook: flush what the agent queued.
	sctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return auto.Shutdown(sctx)
}
