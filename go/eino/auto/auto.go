// Package auto traces CloudWeGo Eino programs for Future AGI from one blank
// import, with no other code:
//
//	import _ "github.com/future-agi/traceAI/go/eino/auto"
//
// The import also links the zero-code agent (github.com/future-agi/traceAI/go/auto),
// so the program needs no second import, and registers the Eino global
// callback handler of github.com/future-agi/traceAI/go/eino. Every graph,
// chain, node, chat model, tool and ADK agent run becomes a span, and the
// spans leave through the agent's route: the application's own global SDK
// provider when it installs one (its own exporters receive them too, and
// Future AGI receives them as AI spans), otherwise the agent's private
// provider. The application never needs a tracer provider of its own.
//
// Everything follows the agent's configuration (FI_API_KEY, FI_SECRET_KEY,
// FI_BASE_URL, FI_PROJECT_NAME, the configuration file, auto.Init options):
//
//   - FI_INSTRUMENTATIONS without "eino" (or with eino in
//     FI_DISABLED_INSTRUMENTATIONS) records nothing;
//   - OTEL_SDK_DISABLED=true records nothing;
//   - the FI_HIDE_* switches and the known-secret replacement apply exactly
//     as to the agent's wire spans;
//   - a program that also calls callbacks.AppendGlobalHandlers(traceaieino.NewHandler())
//     itself gets each span once: this package's handler stops starting spans
//     as soon as the application builds its own;
//   - a chat model's HTTP call under an Eino model span adds no second span,
//     because the agent never records a wire span beneath a span that already
//     carries an LLM, EMBEDDING, RERANKER or GUARDRAIL kind.
//
// Go has no exit hook: call auto.Shutdown(ctx) before main returns so queued
// spans are exported.
package auto

import (
	"context"
	"sync"
	"sync/atomic"

	"github.com/cloudwego/eino/callbacks"
	"github.com/cloudwego/eino/schema"
	"github.com/future-agi/traceAI/go/auto"
	traceaieino "github.com/future-agi/traceAI/go/eino"
)

func init() {
	h := &handler{att: auto.Attach("eino")}
	// An ADK agent's span ends when its event stream has been read to the end,
	// in the background: wait for those before the agent flushes.
	h.att.OnShutdown(func(ctx context.Context) error {
		if in := h.inner.Load(); in != nil {
			return in.Shutdown(ctx)
		}
		return nil
	})
	callbacks.AppendGlobalHandlers(h)
}

// markKey carries, on the context an OnStart returned, the RunInfo of the run
// whose span this handler started, so the later timings of that run are
// forwarded even if the application builds its own handler meanwhile.
type markKey struct{}

type handler struct {
	att   *auto.Attachment
	mu    sync.Mutex
	inner atomic.Pointer[traceaieino.Handler]
}

var _ callbacks.Handler = (*handler)(nil)

// get returns the Eino handler once the agent is enabled for Eino, building it
// then with the agent's route and privacy configuration.
func (h *handler) get() *traceaieino.Handler {
	if in := h.inner.Load(); in != nil {
		return in
	}
	if !h.att.Enabled() {
		return nil
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if in := h.inner.Load(); in != nil {
		return in
	}
	in := traceaieino.NewDefaultHandler(
		traceaieino.WithTracerProvider(h.att.TracerProvider()),
		traceaieino.WithTraceConfig(traceaieino.TraceConfig(h.att.TraceConfig())),
	)
	h.inner.Store(in)
	return in
}

func owned(ctx context.Context, info *callbacks.RunInfo) bool {
	return ctx != nil && ctx.Value(markKey{}) == info
}

// forward returns the Eino handler for a timing of a run, or nil when the
// timing is to be ignored: an application-built handler exists and this
// handler did not start the run.
func (h *handler) forward(ctx context.Context, info *callbacks.RunInfo) *traceaieino.Handler {
	if !owned(ctx, info) && traceaieino.ManualHandlers() > 0 {
		// The application's handler records; the agent must still be active,
		// so the application's own provider forwards those spans to Future AGI
		// from the first one.
		h.att.Enabled()
		return nil
	}
	return h.get()
}

func (h *handler) OnStart(ctx context.Context, info *callbacks.RunInfo, input callbacks.CallbackInput) context.Context {
	in := h.forward(ctx, info)
	if in == nil {
		return ctx
	}
	return context.WithValue(in.OnStart(ctx, info, input), markKey{}, info)
}

func (h *handler) OnEnd(ctx context.Context, info *callbacks.RunInfo, output callbacks.CallbackOutput) context.Context {
	if in := h.forward(ctx, info); in != nil {
		return in.OnEnd(ctx, info, output)
	}
	return ctx
}

func (h *handler) OnError(ctx context.Context, info *callbacks.RunInfo, err error) context.Context {
	if in := h.forward(ctx, info); in != nil {
		return in.OnError(ctx, info, err)
	}
	return ctx
}

func (h *handler) OnStartWithStreamInput(ctx context.Context, info *callbacks.RunInfo, input *schema.StreamReader[callbacks.CallbackInput]) context.Context {
	in := h.forward(ctx, info)
	if in == nil {
		if input != nil {
			input.Close()
		}
		return ctx
	}
	return context.WithValue(in.OnStartWithStreamInput(ctx, info, input), markKey{}, info)
}

func (h *handler) OnEndWithStreamOutput(ctx context.Context, info *callbacks.RunInfo, output *schema.StreamReader[callbacks.CallbackOutput]) context.Context {
	if in := h.forward(ctx, info); in != nil {
		return in.OnEndWithStreamOutput(ctx, info, output)
	}
	if output != nil {
		output.Close()
	}
	return ctx
}
