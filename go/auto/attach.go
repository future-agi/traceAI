package auto

import (
	"context"
	"fmt"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
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

// runShutdownHooks runs the hooks with their own deadline, half of what is
// left of ctx, so that flushing the queued spans always keeps the other half.
// A hook that ignores its context is abandoned at that deadline.
func runShutdownHooks(ctx context.Context) {
	shutdownHooks.Lock()
	fns := append([]func(context.Context) error(nil), shutdownHooks.fns...)
	shutdownHooks.Unlock()
	if len(fns) == 0 {
		return
	}
	budget := 2500 * time.Millisecond
	if deadline, ok := ctx.Deadline(); ok {
		budget = time.Until(deadline) / 2
	}
	hctx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		for _, fn := range fns {
			_ = guarded(func() error { return fn(hctx) })
		}
	}()
	select {
	case <-done:
	case <-hctx.Done():
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

// markAttached relabels attached modules in a report, with the agent lock
// held. A module is reported as auto only while the agent can serve it: active,
// or armed at package initialization.
func markAttached(r *AgentReport) {
	if agent.current == nil && agent.armed == nil {
		return
	}
	for i := range r.Integrations {
		if _, ok := attached.Load(r.Integrations[i].Name); ok && r.Integrations[i].Status == "manual" {
			r.Integrations[i].Status = "auto"
			r.Integrations[i].Reason = "attached to the agent by its blank-import package"
		}
	}
}

// route returns the active agent state this module may use, activating the
// agent on first use. It reads the configuration first and returns nil, without
// activating anything, when the agent is disabled, the module is not allowed or
// the configuration cannot be read; a later Init then still applies its options.
// Activation from a module never wraps http.DefaultTransport: only an agent
// armed at package initialization captures wire spans. It returns nil too when
// the agent is shut down or does not serve the module.
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
			if c, err := readConfig(); err == nil && !c.Disabled && allowed(c, a.name) {
				_, _ = activate(context.Background(), settings{module: true}, false)
			}
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
// A module uses it to wait for spans it still finishes in the background. The
// hooks share half of the shutdown budget; fn must respect its context, and a
// hook still running when that half is spent is abandoned. Its error and
// panics are ignored.
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
// at the agent's first activation (so the application's own exporters receive
// them, and Future AGI receives them as AI spans), otherwise the agent's
// private provider. The provider is fixed at that activation. Until then each
// Start looks again, and the tracers record nothing while the agent is
// disabled, shut down or does not allow the module. Spans keep their parent
// from the context.
//
// Every FI_HIDE_* switch applies to the attributes of these spans as it does
// to the agent's wire spans, including the copy the application's own
// exporters receive. Known-secret replacement applies only to the copy sent to
// Future AGI.
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
	s := t.a.route()
	if s == nil {
		return noop.NewTracerProvider().Tracer(t.name).Start(ctx, spanName, opts...)
	}
	mask := s.config.TraceConfig.Mask
	cfg := trace.NewSpanStartConfig(opts...)
	masked := []trace.SpanStartOption{
		trace.WithAttributes(maskAttributes(mask, cfg.Attributes())...),
		trace.WithLinks(cfg.Links()...),
		trace.WithSpanKind(cfg.SpanKind()),
	}
	if !cfg.Timestamp().IsZero() {
		masked = append(masked, trace.WithTimestamp(cfg.Timestamp()))
	}
	if cfg.NewRoot() {
		masked = append(masked, trace.WithNewRoot())
	}
	ctx, span := s.provider.Tracer(t.name, t.opts...).Start(ctx, spanName, masked...)
	return ctx, &maskedSpan{Span: span, mask: mask}
}

func maskAttributes(mask func(attribute.KeyValue) (attribute.KeyValue, bool), kvs []attribute.KeyValue) []attribute.KeyValue {
	out := make([]attribute.KeyValue, 0, len(kvs))
	for _, kv := range kvs {
		if m, keep := mask(kv); keep {
			out = append(out, m)
		}
	}
	return out
}

// maskedSpan applies the agent's privacy switches to everything written to a
// module span after it started.
type maskedSpan struct {
	trace.Span
	mask func(attribute.KeyValue) (attribute.KeyValue, bool)
}

func (s *maskedSpan) SetAttributes(kvs ...attribute.KeyValue) {
	s.Span.SetAttributes(maskAttributes(s.mask, kvs)...)
}

func (s *maskedSpan) AddEvent(name string, opts ...trace.EventOption) {
	cfg := trace.NewEventConfig(opts...)
	s.Span.AddEvent(name, trace.WithAttributes(maskAttributes(s.mask, cfg.Attributes())...), trace.WithTimestamp(cfg.Timestamp()))
}

func (s *maskedSpan) RecordError(err error, opts ...trace.EventOption) {
	if err == nil {
		return
	}
	cfg := trace.NewEventConfig(opts...)
	kvs := append([]attribute.KeyValue{attribute.String("exception.type", fmt.Sprintf("%T", err)), attribute.String("exception.message", err.Error())}, cfg.Attributes()...)
	s.Span.AddEvent("exception", trace.WithAttributes(maskAttributes(s.mask, kvs)...), trace.WithTimestamp(cfg.Timestamp()))
}

func (s *maskedSpan) SetStatus(code codes.Code, description string) {
	if code == codes.Error {
		if m, keep := s.mask(attribute.String("error.message", description)); keep {
			description = m.Value.AsString()
		} else {
			description = ""
		}
	}
	s.Span.SetStatus(code, description)
}
