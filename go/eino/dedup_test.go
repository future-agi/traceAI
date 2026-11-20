package traceaieino

import (
	"context"
	"runtime"
	"testing"
	"time"

	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/components"
	"github.com/cloudwego/eino/schema"
)

func runInfo() *callbacks.RunInfo {
	return &callbacks.RunInfo{Name: "step", Type: "Fake", Component: components.ComponentOfChatModel}
}

// A handler an application builds marks the runs Eino offers it. Eino asks
// every handler of a run whether it needs a timing, application handlers
// before global ones, so a default handler can leave exactly those runs alone.
func TestApplicationHandlerMarksTheRunsItIsOfferedAndDefaultHandlerYields(t *testing.T) {
	manual, def := NewHandler(), NewDefaultHandler()
	mine, other := runInfo(), runInfo()
	if HandledManually(mine) {
		t.Fatal("a run is marked before any handler was offered it")
	}
	if !def.Needed(context.Background(), mine, callbacks.TimingOnStart) {
		t.Fatal("the default handler declines a run no application handler was offered")
	}
	if !manual.Needed(context.Background(), mine, callbacks.TimingOnStart) {
		t.Fatal("an application handler declines a timing")
	}
	if !HandledManually(mine) || HandledManually(other) {
		t.Fatalf("marks: mine=%v other=%v", HandledManually(mine), HandledManually(other))
	}
	if def.Needed(context.Background(), mine, callbacks.TimingOnEnd) {
		t.Fatal("the default handler stays on a run an application handler handles")
	}
	if !def.Needed(context.Background(), other, callbacks.TimingOnStart) {
		t.Fatal("the default handler left a run no application handler handles")
	}
}

// Building a handler that is never used changes nothing.
func TestBuildingAHandlerMarksNoRun(t *testing.T) {
	_ = NewHandler()
	if HandledManually(runInfo()) {
		t.Fatal("a built handler marked a run")
	}
}

func TestMarksAreForgottenWhenTheRunIsCollected(t *testing.T) {
	manual := NewHandler()
	for i := 0; i < 50; i++ {
		manual.Needed(context.Background(), runInfo(), callbacks.TimingOnStart)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		runtime.GC()
		n := 0
		manualRuns.Range(func(_, _ any) bool { n++; return true })
		if n == 0 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("marks of finished runs are kept forever")
}

// RunTraced tells a later handler that an earlier one already started the
// span of this very run, not of an enclosing one.
func TestRunTracedSeesOnlyTheRunsOwnSpan(t *testing.T) {
	_, _, h := recorder(t, TraceConfig{})
	info := runInfo()
	ctx := h.OnStart(context.Background(), info, &schema.Message{})
	if !RunTraced(ctx, info) {
		t.Fatal("a started run is not reported as traced")
	}
	if RunTraced(ctx, runInfo()) {
		t.Fatal("a nested run is reported as traced by its parent's span")
	}
	if RunTraced(context.Background(), info) {
		t.Fatal("a bare context reports a traced run")
	}
}
