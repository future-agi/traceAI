package auto

import (
	"context"
	"reflect"
	"runtime"
	"sync"
	"sync/atomic"
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

// runHooks runs the registered hooks once per agent, outside its shutdown
// Once, and waits for them for at most half of what is left of this caller's
// context, so flushing the queued spans always keeps the other half and no
// caller waits longer than its own deadline allows. The first caller starts
// the hooks with a context of that same budget; a hook that ignores it is
// abandoned when it ends.
func (s *agentState) runHooks(ctx context.Context) {
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
	if s.hooksStarted.CompareAndSwap(false, true) {
		hctx, cancel := context.WithTimeout(ctx, budget)
		go func() {
			defer cancel()
			defer close(s.hooksDone)
			for _, fn := range fns {
				_ = guarded(func() error { return fn(hctx) })
			}
		}()
	}
	timer := time.NewTimer(budget)
	defer timer.Stop()
	select {
	case <-s.hooksDone:
	case <-ctx.Done():
	case <-timer.C:
	}
}

// moduleState is the agent's active state for dedicated modules: read without
// the agent lock once the agent is active.
var moduleState atomic.Pointer[agentState]

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
	if s := moduleState.Load(); s != nil && s.active.Load() {
		// The agent is active: no lock, however many runs are in flight.
		if allowed(s.config, a.name) {
			return s
		}
		return nil
	}
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
// exporters receive. Known secrets are replaced in the copy the application's
// own exporters receive (with the credentials known as each value is written
// and again as the span ends) and, from the values before that replacement, in
// the copy sent to Future AGI at export, so a longer secret learned after a
// shorter overlapping one still matches as a whole.
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
	m := moduleMask{cfg: s.config}
	cfg := trace.NewSpanStartConfig(opts...)
	links := make([]trace.Link, 0, len(cfg.Links()))
	for _, l := range cfg.Links() {
		links = append(links, trace.Link{SpanContext: l.SpanContext, Attributes: m.attributes(l.Attributes)})
	}
	raw := m.captured(cfg.Attributes())
	written := m.replaced(raw)
	rawName := captureString(spanName, s.config)
	masked := []trace.SpanStartOption{
		trace.WithAttributes(written...),
		trace.WithLinks(links...),
		trace.WithSpanKind(cfg.SpanKind()),
	}
	if !cfg.Timestamp().IsZero() {
		masked = append(masked, trace.WithTimestamp(cfg.Timestamp()))
	}
	if cfg.NewRoot() {
		masked = append(masked, trace.WithNewRoot())
	}
	ctx, span := s.provider.Tracer(t.name, t.opts...).Start(ctx, m.text(rawName), masked...)
	// The context carries the masked span too, so a module that reads the span back from it writes through the same rules.
	ms := &maskedSpan{Span: span, m: m, reg: s.registration, applied: map[attribute.Key]string{}, raw: map[attribute.Key]attribute.Value{}}
	ms.remember(raw, written, &rawName)
	return trace.ContextWithSpan(ctx, ms), ms
}

// moduleMask gives a module's span attributes exactly the treatment the agent's
// wire attributes get: string values pass the structural capture (which removes
// image references when they are hidden), then every FI_HIDE_* switch applies.
// Known credentials are replaced afterwards in the copy the span's own provider
// exports, with the snapshot of the moment (see maskedSpan); the copy sent to
// Future AGI starts from the values without that replacement and takes the
// export-time snapshot, as the agent's wire spans do.
type moduleMask struct{ cfg Config }

// text replaces the agent's known credentials in s.
func (m moduleMask) text(s string) string { return processSecrets.snapshot().replace(s) }

// attribute applies the structural capture and the privacy switches to one attribute.
func (m moduleMask) attribute(kv attribute.KeyValue) (attribute.KeyValue, bool) {
	switch kv.Value.Type() {
	case attribute.STRING:
		kv = attribute.String(string(kv.Key), captureString(kv.Value.AsString(), m.cfg))
	case attribute.STRINGSLICE:
		values := append([]string(nil), kv.Value.AsStringSlice()...)
		for i, v := range values {
			values[i] = captureString(v, m.cfg)
		}
		kv = attribute.StringSlice(string(kv.Key), values)
	}
	return m.cfg.TraceConfig.Mask(kv)
}

// captured is the attributes after the structural capture and the privacy switches.
func (m moduleMask) captured(kvs []attribute.KeyValue) []attribute.KeyValue {
	out := make([]attribute.KeyValue, 0, len(kvs))
	for _, kv := range kvs {
		if masked, keep := m.attribute(kv); keep {
			out = append(out, masked)
		}
	}
	return out
}

// replaced replaces the agent's known credentials in string values.
func (m moduleMask) replaced(kvs []attribute.KeyValue) []attribute.KeyValue {
	snapshot := processSecrets.snapshot()
	out := make([]attribute.KeyValue, len(kvs))
	for i, kv := range kvs {
		switch kv.Value.Type() {
		case attribute.STRING:
			kv = attribute.String(string(kv.Key), snapshot.replace(kv.Value.AsString()))
		case attribute.STRINGSLICE:
			values := append([]string(nil), kv.Value.AsStringSlice()...)
			for j, v := range values {
				values[j] = snapshot.replace(v)
			}
			kv = attribute.StringSlice(string(kv.Key), values)
		}
		out[i] = kv
	}
	return out
}

// attributes is captured and replaced, for what is written once (links, events).
func (m moduleMask) attributes(kvs []attribute.KeyValue) []attribute.KeyValue {
	return m.replaced(m.captured(kvs))
}

// event masks an event's name and attributes. While any content switch is on,
// everything but the exception name and exception.type is replaced by the
// redaction marker: an event can carry anything the module put in it.
func (m moduleMask) event(name string, kvs []attribute.KeyValue) (string, []attribute.KeyValue) {
	if !hidesContent(m.cfg.TraceConfig) {
		return m.text(captureString(name, m.cfg)), m.attributes(kvs)
	}
	if name != "exception" {
		name = RedactedValue
	}
	out := make([]attribute.KeyValue, 0, len(kvs))
	for _, kv := range kvs {
		switch {
		case kv.Key == "exception.type":
			out = append(out, attribute.String("exception.type", m.text(captureString(kv.Value.Emit(), m.cfg))))
		default:
			out = append(out, attribute.String(string(kv.Key), RedactedValue))
		}
	}
	return name, out
}

// maskedSpan applies the agent's privacy rules to everything written to a
// module span after it started.
//
// Known credentials are replaced in the span's attributes and name as they are
// written, and again when the span ends, from the values kept without that
// replacement, so the application's own exporters, which read the span as it
// ends, receive the replacement of the end. The raw values stay in the span's
// record in the agent (never on the span), and the export to Future AGI starts
// from them: a longer secret learned after a shorter overlapping one was
// replaced still matches as a whole. Events and links keep the replacement of
// the moment they were written.
type maskedSpan struct {
	trace.Span
	m   moduleMask
	reg *processorRegistration
	mu  sync.Mutex
	raw map[attribute.Key]attribute.Value // before replacement
	// applied is what was last written to the span for each key.
	applied map[attribute.Key]string
	name    *string
}

// remember keeps raw values (and the raw name when not nil) for the end and for the export to Future AGI, and
// what was written to the span for them.
func (s *maskedSpan) remember(kvs, written []attribute.KeyValue, name *string) {
	s.mu.Lock()
	for _, kv := range kvs {
		s.raw[kv.Key] = kv.Value
	}
	for _, kv := range written {
		s.applied[kv.Key] = kv.Value.Emit()
	}
	if name != nil {
		s.name = name
	}
	s.mu.Unlock()
	if s.reg != nil {
		s.reg.rememberRaw(s.Span.SpanContext(), kvs, name)
	}
}

func (s *maskedSpan) SetAttributes(kvs ...attribute.KeyValue) {
	raw := s.m.captured(kvs)
	written := s.m.replaced(raw)
	s.remember(raw, written, nil)
	s.Span.SetAttributes(written...)
}

// End replaces the credentials known by now in the attributes and the name, then ends the span.
func (s *maskedSpan) End(opts ...trace.SpanEndOption) {
	if s.Span.IsRecording() {
		s.mu.Lock()
		var changed []attribute.KeyValue
		for _, kv := range s.m.replaced(rawList(s.raw)) {
			if kv.Value.Emit() != s.applied[kv.Key] {
				changed = append(changed, kv)
				s.applied[kv.Key] = kv.Value.Emit()
			}
		}
		name := s.name
		s.mu.Unlock()
		if len(changed) > 0 {
			s.Span.SetAttributes(changed...)
		}
		if name != nil {
			s.Span.SetName(s.m.text(*name))
		}
	}
	s.Span.End(opts...)
}

func rawList(raw map[attribute.Key]attribute.Value) []attribute.KeyValue {
	out := make([]attribute.KeyValue, 0, len(raw))
	for k, v := range raw {
		out = append(out, attribute.KeyValue{Key: k, Value: v})
	}
	return out
}

func (s *maskedSpan) AddEvent(name string, opts ...trace.EventOption) {
	cfg := trace.NewEventConfig(opts...)
	name, kvs := s.m.event(name, cfg.Attributes())
	s.Span.AddEvent(name, trace.WithAttributes(kvs...), trace.WithTimestamp(cfg.Timestamp()))
}

// RecordError writes the exception event the SDK writes: exception.type, exception.message and, when asked,
// exception.stacktrace, through the same rules as every other event.
func (s *maskedSpan) RecordError(err error, opts ...trace.EventOption) {
	if err == nil {
		return
	}
	cfg := trace.NewEventConfig(opts...)
	kvs := []attribute.KeyValue{attribute.String("exception.type", errorType(err)), attribute.String("exception.message", err.Error())}
	if cfg.StackTrace() {
		kvs = append(kvs, attribute.String("exception.stacktrace", stackTrace()))
	}
	kvs = append(kvs, cfg.Attributes()...)
	name, masked := s.m.event("exception", kvs)
	s.Span.AddEvent(name, trace.WithAttributes(masked...), trace.WithTimestamp(cfg.Timestamp()))
}

// errorType is exception.type as the SDK builds it: the package path and name of a named type, else its string form.
func errorType(err error) string {
	t := reflect.TypeOf(err)
	if t.PkgPath() == "" && t.Name() == "" {
		return t.String()
	}
	return t.PkgPath() + "." + t.Name()
}

// stackTrace is the calling goroutine's stack, as the SDK records it for WithStackTrace(true).
func stackTrace() string {
	buf := make([]byte, 2048)
	for {
		n := runtime.Stack(buf, false)
		if n < len(buf) {
			return string(buf[:n])
		}
		buf = make([]byte, 2*len(buf))
	}
}

// SetStatus keeps the description as the wire span's own status does, through
// the structural capture only: the go/eino handler sets nothing but the Go
// error type there, and the message is a masked attribute.
func (s *maskedSpan) SetStatus(code codes.Code, description string) {
	if code == codes.Error {
		description = s.m.text(captureString(description, s.m.cfg))
	}
	s.Span.SetStatus(code, description)
}

// SetName gives a rename the treatment the name given at start gets.
func (s *maskedSpan) SetName(name string) {
	raw := captureString(name, s.m.cfg)
	s.remember(nil, nil, &raw)
	s.Span.SetName(s.m.text(raw))
}

// AddLink gives a late link the treatment the links given at start get.
func (s *maskedSpan) AddLink(link trace.Link) {
	s.Span.AddLink(trace.Link{SpanContext: link.SpanContext, Attributes: s.m.attributes(link.Attributes)})
}
