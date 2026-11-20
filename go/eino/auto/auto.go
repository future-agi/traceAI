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
//   - the FI_HIDE_* switches apply exactly as to the agent's wire spans, to
//     the copy Future AGI receives and the copy the application's own
//     provider exports (error.message becomes the redaction marker rather
//     than being dropped); known secrets are replaced in both on every part
//     of a span (attributes, name, events, links, status), in the
//     application's copy as each span ends and in Future AGI's at export;
//   - a run that an application handler (traceaieino.NewHandler(), registered
//     globally or given to one run with compose.WithCallbacks or
//     adk.WithCallbacks) handles gets its spans from that handler only; every
//     other run keeps being traced here. Building a handler without using it
//     changes nothing;
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

// handler is registered globally at start-up and forwards to a go/eino default
// handler built, on first use, with the agent's route and privacy configuration.
type handler struct {
	att   *auto.Attachment
	mu    sync.Mutex
	inner atomic.Pointer[traceaieino.Handler]
	idle  atomic.Pointer[traceaieino.Handler]
}

var (
	_ callbacks.Handler       = (*handler)(nil)
	_ callbacks.TimingChecker = (*handler)(nil)
)

// Needed declines a run the application handles with its own handler, and every
// run while the agent does not serve Eino (not allowed, disabled, not
// configured): Eino then makes no copy of a stream for this handler.
func (h *handler) Needed(_ context.Context, info *callbacks.RunInfo, _ callbacks.CallbackTiming) bool {
	return traceaieino.DefaultNeeded(info, h.att.Enabled)
}

// target returns the handler to forward to. When the agent does not serve Eino
// it is a handler shut down from the start, which records nothing but still
// releases the streams Eino hands it.
func (h *handler) target() *traceaieino.Handler {
	if in := h.inner.Load(); in != nil {
		return in
	}
	if h.att.Enabled() {
		h.mu.Lock()
		defer h.mu.Unlock()
		if in := h.inner.Load(); in != nil {
			return in
		}
		in := traceaieino.NewDefaultHandler(
			traceaieino.WithTracerProvider(h.att.TracerProvider()),
			// The route applies the agent's privacy rules to every span attribute once (see auto.Attachment.TracerProvider),
			// so the handler applies no hide switch of its own; it keeps only the size cap for inline images.
			traceaieino.WithTraceConfig(traceaieino.TraceConfig{Base64ImageMaxLength: h.att.TraceConfig().Base64ImageMaxLength}),
		)
		h.inner.Store(in)
		return in
	}
	if idle := h.idle.Load(); idle != nil {
		return idle
	}
	idle := traceaieino.NewDefaultHandler()
	_ = idle.Shutdown(context.Background())
	h.idle.CompareAndSwap(nil, idle)
	return h.idle.Load()
}

func (h *handler) OnStart(ctx context.Context, info *callbacks.RunInfo, input callbacks.CallbackInput) context.Context {
	return h.target().OnStart(ctx, info, input)
}

func (h *handler) OnEnd(ctx context.Context, info *callbacks.RunInfo, output callbacks.CallbackOutput) context.Context {
	return h.target().OnEnd(ctx, info, output)
}

func (h *handler) OnError(ctx context.Context, info *callbacks.RunInfo, err error) context.Context {
	return h.target().OnError(ctx, info, err)
}

func (h *handler) OnStartWithStreamInput(ctx context.Context, info *callbacks.RunInfo, input *schema.StreamReader[callbacks.CallbackInput]) context.Context {
	return h.target().OnStartWithStreamInput(ctx, info, input)
}

func (h *handler) OnEndWithStreamOutput(ctx context.Context, info *callbacks.RunInfo, output *schema.StreamReader[callbacks.CallbackOutput]) context.Context {
	return h.target().OnEndWithStreamOutput(ctx, info, output)
}
