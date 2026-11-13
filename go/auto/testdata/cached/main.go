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

// This runs after auto's init but before main: the startup pattern that used to
// leave the application's cached tracer attached to a retired SDK provider.
var cached = otel.Tracer("cached-during-package-init")
var initialProvider = otel.GetTracerProvider()

func main() {
	recorder := tracetest.NewSpanRecorder()
	// Observe cached tracers through the initial provider. Init options no
	// longer replace the blank-import processor or its configuration.
	initialProvider.(*sdktrace.TracerProvider).RegisterSpanProcessor(recorder)
	stop, err := auto.Init(context.Background(), auto.WithProject("explicit-project", "observe"), auto.WithSpanProcessor(recorder))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	_, s := cached.Start(context.Background(), "cached-application-span")
	recording := s.IsRecording()
	s.End()
	_, s = otel.Tracer("fresh").Start(context.Background(), "fresh-application-span")
	s.End()
	var names []string
	for _, span := range recorder.Ended() {
		names = append(names, span.Name())
	}
	_ = stop(context.Background())
	_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"provider_replaced": initialProvider != otel.GetTracerProvider(), "cached_recording": recording, "names": names})
}
