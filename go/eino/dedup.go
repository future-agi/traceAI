package traceaieino

import (
	"context"
	"runtime"
	"sync"
	"weak"

	"github.com/cloudwego/eino/callbacks"
)

// Eino offers every run to its handlers in a fixed order. First it asks each
// handler, through the optional Needed method, whether it wants a timing:
// handlers given to one run (compose.WithCallbacks, adk.WithCallbacks) first,
// then the global ones in registration order. Then it calls OnStart on them in
// reverse order and OnEnd / OnError in order. A handler an application built
// with [NewHandler] marks, in Needed, each run it is offered. A default
// handler (see [NewDefaultHandler]) declines the runs so marked, and, for the
// remaining case of an application handler registered after it, notices in
// OnStart that the run already has a span ([RunTraced]). Building a handler
// that is never registered or passed to a run marks nothing.

// manualRuns holds weak pointers to the RunInfo of runs an application handler
// was offered. An entry is dropped when the run's RunInfo is collected.
var manualRuns sync.Map // weak.Pointer[callbacks.RunInfo] -> struct{}

func markManual(info *callbacks.RunInfo) {
	if info == nil {
		return
	}
	wp := weak.Make(info)
	if _, loaded := manualRuns.LoadOrStore(wp, struct{}{}); !loaded {
		runtime.AddCleanup(info, func(wp weak.Pointer[callbacks.RunInfo]) { manualRuns.Delete(wp) }, wp)
	}
}

// HandledManually reports whether a handler built with [NewHandler] was offered
// the run described by info, that is, whether it is registered globally or was
// given to this run. It is false for a handler that was only built.
func HandledManually(info *callbacks.RunInfo) bool {
	if info == nil {
		return false
	}
	_, ok := manualRuns.Load(weak.Make(info))
	return ok
}

// RunTraced reports whether a [Handler] has already started the span of the
// run described by info and left it in ctx. It is false for a span of an
// enclosing run.
func RunTraced(ctx context.Context, info *callbacks.RunInfo) bool {
	if ctx == nil {
		return false
	}
	st := running(ctx, info)
	return st != nil && st.span != nil
}

// defaultRuns holds, per run, the answer a default handler gave the first time
// Eino asked whether it wants a timing of that run.
var defaultRuns sync.Map // weak.Pointer[callbacks.RunInfo] -> bool

// DefaultNeeded is the answer of a default handler to Eino's Needed question
// for the run described by info: yes unless an application handler handles the
// run, and only while serve reports that the handler is in service. The answer
// is fixed at the first timing offered for the run, because the span of a run
// that started must also be ended: an application handler registered
// mid-run, or a handler that is shut down, changes the answer for later runs
// only.
func DefaultNeeded(info *callbacks.RunInfo, serve func() bool) bool {
	if info == nil {
		return serve()
	}
	wp := weak.Make(info)
	if v, ok := defaultRuns.Load(wp); ok {
		return v.(bool)
	}
	// serve is asked first, also for a run an application handler handles: it
	// activates the agent, so the application's own provider forwards that
	// handler's spans to Future AGI from the run's first span.
	in := serve()
	need := in && !HandledManually(info)
	if v, loaded := defaultRuns.LoadOrStore(wp, need); loaded {
		return v.(bool)
	}
	runtime.AddCleanup(info, func(wp weak.Pointer[callbacks.RunInfo]) { defaultRuns.Delete(wp) }, wp)
	return need
}

// Needed implements callbacks.TimingChecker. An application handler wants every
// timing and marks the run; a default handler wants every timing of the runs no
// application handler handles.
func (h *Handler) Needed(_ context.Context, info *callbacks.RunInfo, _ callbacks.CallbackTiming) bool {
	if !h.deflt {
		markManual(info)
		return true
	}
	return DefaultNeeded(info, func() bool { return !h.closed.Load() })
}
