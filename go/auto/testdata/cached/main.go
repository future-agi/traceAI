package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// These run after the agent's package initialization but before main: the
// usual place for an application to cache its tracer.
var cached = otel.Tracer("cached-during-package-init")
var initialProvider = otel.GetTracerProvider()

func names(recorder *tracetest.SpanRecorder) []string {
	out := []string{}
	for _, span := range recorder.Ended() {
		out = append(out, span.Name())
	}
	return out
}

func main() {
	// The application installs its own provider in main, then initializes the
	// agent explicitly before its first AI call, so the options apply.
	application := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(application))
	otel.SetTracerProvider(provider)
	agent := tracetest.NewSpanRecorder()
	stop, err := auto.Init(context.Background(), auto.WithProject("explicit-project", "observe"), auto.WithSpanProcessor(agent))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	project := auto.Report().Config.ProjectName
	_, s := cached.Start(context.Background(), "cached-application-span")
	s.End()
	_, s = otel.Tracer("fresh").Start(context.Background(), "fresh-application-span")
	s.End()
	_ = stop(context.Background())
	_, installed := initialProvider.(*sdktrace.TracerProvider)
	_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"agent_installed_global": installed, "application": names(application), "agent": names(agent), "project": project})
}
