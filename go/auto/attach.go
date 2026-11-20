package auto

import (
	"context"
	"sync"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/embedded"
	"go.opentelemetry.io/otel/trace/noop"
)

// attached names the dedicated modules whose blank-import package attached to
// the agent. Report shows them as "auto" instead of "manual".
var attached sync.Map

// moduleProbe remembers the application provider seen at the last activation
// attempt made for a module, so a module that cannot be served (no
// credentials, no provider) does not re-read the configuration on every span.
var moduleProbe struct {
	tried bool
	seen  *sdktrace.TracerProvider
}

// shutdownHooks are run by the agent's shutdown, before it stops recording.
var shutdownHooks struct {
	sync.Mutex
	fns []func(context.Context) error
}

func runShutdownHooks(ctx context.Context) {
	shutdownHooks.Lock()
	fns := append([]func(context.Context) error(nil), shutdownHooks.fns...)
	shutdownHooks.Unlock()
	for _, fn := range fns {
		_ = guarded(func() error { return fn(ctx) })
	}
}

// Attachment is a dedicated traceAI module's route to the agent. A module
// package that is meant to work from one blank import (for example
// github.com/future-agi/traceAI/go/eino/auto) calls [Attach] from its package
// initialization and starts its spans from [Attachment.TracerProvider].
type Attachment struct{ name string }

// Attach registers the dedicated module name (a FI_INSTRUMENTATIONS entry such
// as "eino") with the agent and returns its route. It does nothing else:
// activation is lazy, and no exporter or provider exists until the module's
// first span or an explicit Init. Report shows the module as "auto" from now
// on (or "disabled" when the configuration does not allow it).
func Attach(name string) *Attachment {
	attached.Store(name, true)
	return &Attachment{name: name}
}

// markAttached relabels attached modules in a report.
func markAttached(r *AgentReport) {
	for i := range r.Integrations {
		if _, ok := attached.Load(r.Integrations[i].Name); ok && r.Integrations[i].Status == "manual" {
			r.Integrations[i].Status = "auto"
			r.Integrations[i].Reason = "attached to the agent by its blank-import package"
		}
	}
}

// route returns the active agent state this module may use, activating the
// agent on first use exactly as the first captured AI request does. It
// returns nil when the agent is disabled, unconfigured, shut down, or when the
// resolved configuration does not allow the module.
func (a *Attachment) route() (s *agentState) {
	defer func() {
		if recover() != nil {
			warn()
			s = nil
		}
	}()
	agent.Lock()
	defer agent.Unlock()
	if agent.current == nil && agent.first == nil {
		global, _ := otel.GetTracerProvider().(*sdktrace.TracerProvider)
		if !moduleProbe.tried || moduleProbe.seen != global {
			moduleProbe.tried, moduleProbe.seen = true, global
			_, _ = activate(context.Background(), settings{}, false)
		}
	}
	s = agent.current
	if s == nil || !s.active.Load() || !allowed(s.config, a.name) {
		return nil
	}
	return s
}

// OnShutdown registers fn to run when the agent shuts down (auto.Shutdown or
// the function Init returned), before the agent stops recording and flushes.
// A module uses it to wait for spans it still finishes in the background. fn
// must respect ctx's deadline; its error and panics are ignored.
func (a *Attachment) OnShutdown(fn func(context.Context) error) {
	shutdownHooks.Lock()
	shutdownHooks.fns = append(shutdownHooks.fns, fn)
	shutdownHooks.Unlock()
}

// Enabled reports whether the module's spans are currently recorded, which
// activates the agent if that has not happened yet.
func (a *Attachment) Enabled() bool { return a.route() != nil }

// TraceConfig is the agent's resolved privacy configuration: the FI_HIDE_*
// environment, the configuration file and code options, in the agent's order
// of precedence. It is the zero value (nothing hidden) while the module is not
// enabled, because nothing is recorded then.
func (a *Attachment) TraceConfig() TraceConfig {
	if s := a.route(); s != nil {
		return s.config.TraceConfig
	}
	return TraceConfig{}
}

// TracerProvider returns a provider whose tracers start their spans on the
// agent's route: the application's global SDK provider when one is installed
// (so the application's own exporters receive them, and Future AGI receives
// them as AI spans), otherwise the agent's private provider. The route is
// resolved at every Start, so a provider the application installs later is
// used, and the tracers record nothing while the agent is disabled, shut down
// or does not allow the module. Spans keep their parent from the context.
func (a *Attachment) TracerProvider() trace.TracerProvider { return moduleProvider{a: a} }

type moduleProvider struct {
	embedded.TracerProvider
	a *Attachment
}

func (p moduleProvider) Tracer(name string, opts ...trace.TracerOption) trace.Tracer {
	return &moduleTracer{a: p.a, name: name, opts: opts}
}

type moduleTracer struct {
	embedded.Tracer
	a    *Attachment
	name string
	opts []trace.TracerOption
}

func (t *moduleTracer) Start(ctx context.Context, spanName string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	if s := t.a.route(); s != nil {
		return s.provider.Tracer(t.name, t.opts...).Start(ctx, spanName, opts...)
	}
	return noop.NewTracerProvider().Tracer(t.name).Start(ctx, spanName, opts...)
}
