package auto

import (
	"context"
	"reflect"
	"runtime"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

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
// exporters receive. Known secrets are replaced on every surface of the span
// (attributes, name, events, links, status) in the copy the application's own
// exporters receive, with the credentials known as the span ends (its events
// and links are added then), and, from what the module wrote before that
// replacement, in the copy sent to Future AGI with the credentials known at
// export, so a longer secret learned after a shorter overlapping one still
// matches as a whole on both.
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
	raw := m.captured(cfg.Attributes())
	written := m.replaced(raw)
	rawName := captureString(spanName, s.config)
	// Links are not given to the SDK here: the span records them when it ends, with the credentials known then.
	masked := []trace.SpanStartOption{
		trace.WithAttributes(written...),
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
	ms := &maskedSpan{Span: span, m: m, reg: s.registration, caps: readSpanCaps(s.provider)}
	ms.keep(raw, written, &rawName)
	for _, l := range cfg.Links() {
		ms.addLink(trace.Link{SpanContext: l.SpanContext, Attributes: l.Attributes})
	}
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

// event masks an event's name and attributes, without replacing credentials. While any content switch is on,
// everything but the exception name and exception.type is replaced by the redaction marker: an event can carry
// anything the module put in it.
func (m moduleMask) event(name string, kvs []attribute.KeyValue) (string, []attribute.KeyValue) {
	if !hidesContent(m.cfg.TraceConfig) {
		return captureString(name, m.cfg), m.captured(kvs)
	}
	if name != "exception" {
		name = RedactedValue
	}
	out := make([]attribute.KeyValue, 0, len(kvs))
	for _, kv := range kvs {
		switch {
		case kv.Key == "exception.type":
			out = append(out, attribute.String("exception.type", captureString(kv.Value.Emit(), m.cfg)))
		default:
			out = append(out, attribute.String(string(kv.Key), RedactedValue))
		}
	}
	return name, out
}

// maskedSpan applies the agent's privacy rules to everything written to a module span after it started.
//
// What the module writes is kept in the span's record in the agent (never on the span), before known credentials are
// replaced: attributes (those the replacement changes), the name, and, because the SDK cannot rewrite them once added,
// every event, every link and the status. The span itself carries the replacement of the moment for attributes, the
// name and the status, and gets its events and links when it ends. When it ends, the attributes, name and status are
// replaced again and the events and links are added, all with the credentials known by then: the application's own
// exporters read the span as it ends, so that is the best they can get. The export to Future AGI starts from the
// record (see rawSpan) and takes the credentials known at export, so a longer secret learned after a shorter
// overlapping one was seen still matches as a whole. A span that never ends keeps its record until shutdown.
type maskedSpan struct {
	trace.Span
	m    moduleMask
	reg  *processorRegistration
	caps spanCaps
}

// spanCaps are the application provider's limits that decide what the SDK keeps of a span: the attribute value length
// (runes), the event and link counts, and the attribute counts of one event and one link (-1: unlimited, or not readable, in
// which case nothing is bounded).
type spanCaps struct{ value, events, links, eventAttrs, linkAttrs int }

func readSpanCaps(provider *sdktrace.TracerProvider) spanCaps {
	caps := spanCaps{value: spanLimit(reflect.ValueOf(provider).Elem(), "AttributeValueLengthLimit"), events: -1, links: -1, eventAttrs: -1, linkAttrs: -1}
	if n, ok := spanLimitIfReadable(provider, "EventCountLimit"); ok {
		caps.events = n
	}
	if n, ok := spanLimitIfReadable(provider, "LinkCountLimit"); ok {
		caps.links = n
	}
	if n, ok := spanLimitIfReadable(provider, "AttributePerEventCountLimit"); ok {
		caps.eventAttrs = n
	}
	if n, ok := spanLimitIfReadable(provider, "AttributePerLinkCountLimit"); ok {
		caps.linkAttrs = n
	}
	return caps
}

// exceeds reports whether the SDK would cut this value to the attribute value-length limit.
func (c spanCaps) exceeds(v attribute.Value) bool {
	if c.value < 0 {
		return false
	}
	switch v.Type() {
	case attribute.STRING:
		return utf8.RuneCountInString(v.AsString()) > c.value
	case attribute.STRINGSLICE:
		for _, item := range v.AsStringSlice() {
			if utf8.RuneCountInString(item) > c.value {
				return true
			}
		}
	}
	return false
}

// maxBufferedStack bounds a stack trace held in the record. Secrets are replaced in it before it is cut to maxStackTrace, so
// the bound only has to leave room for a secret that straddles that cut.
const maxBufferedStack = 64 << 10

// keep records raw attribute values (those the replacement changed, and the name when not nil) in the span's record.
func (s *maskedSpan) keep(raw, written []attribute.KeyValue, name *string) {
	s.reg.update(s.Span.SpanContext(), func(r *spanRecord) {
		if r.raw == nil {
			r.raw = map[attribute.Key]attribute.Value{}
		}
		for i, kv := range raw {
			// A value the SDK would cut to the length limit is kept whole too: a secret learned after the write may straddle the cut.
			if kv.Value.Emit() != written[i].Value.Emit() || s.caps.exceeds(kv.Value) {
				r.raw[kv.Key] = kv.Value
			} else {
				delete(r.raw, kv.Key)
			}
		}
		if name != nil {
			r.rawName = name
		}
	})
}

func (s *maskedSpan) SetAttributes(kvs ...attribute.KeyValue) {
	raw := s.m.captured(kvs)
	written := s.m.replaced(raw)
	s.keep(raw, written, nil)
	s.Span.SetAttributes(written...)
}

func (s *maskedSpan) AddEvent(name string, opts ...trace.EventOption) {
	cfg := trace.NewEventConfig(opts...)
	s.addEvent(name, cfg.Attributes(), cfg.Timestamp())
}

func (s *maskedSpan) addEvent(name string, kvs []attribute.KeyValue, at time.Time) {
	if at.IsZero() {
		at = time.Now()
	}
	rawName, raw := s.m.event(name, kvs)
	for i, kv := range raw {
		if kv.Key == "exception.stacktrace" && kv.Value.Type() == attribute.STRING {
			raw = append([]attribute.KeyValue(nil), raw...)
			raw[i].Value = attribute.StringValue(capStackTo(kv.Value.AsString(), maxBufferedStack))
		}
	}
	raw = limitAttributes(raw, s.caps.eventAttrs)
	s.reg.update(s.Span.SpanContext(), func(r *spanRecord) {
		if r.ending {
			return
		}
		r.events = append(r.events, rawEvent{name: rawName, attrs: raw, time: at})
		// Like the SDK's queue: past the count limit the oldest event goes.
		if over := len(r.events) - s.caps.events; s.caps.events >= 0 && over > 0 {
			r.events = append([]rawEvent(nil), r.events[over:]...)
			r.eventsDropped += over
		}
	})
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
	s.addEvent("exception", append(kvs, cfg.Attributes()...), cfg.Timestamp())
}

// errorType is exception.type as the SDK builds it: the package path and name of a named type, else its string form.
func errorType(err error) string {
	t := reflect.TypeOf(err)
	if t.PkgPath() == "" && t.Name() == "" {
		return t.String()
	}
	return t.PkgPath() + "." + t.Name()
}

// stackTrace is the calling goroutine's whole stack (up to 1 MiB): credentials are replaced in it before it is cut to
// the size the SDK records, so a cut cannot leave a fragment of one.
func stackTrace() string {
	buf := make([]byte, 4096)
	for {
		n := runtime.Stack(buf, false)
		if n < len(buf) || len(buf) >= 1<<20 {
			return string(buf[:n])
		}
		buf = make([]byte, 2*len(buf))
	}
}

// SetStatus keeps the description as the wire span's own status does, through the structural capture only: the
// go/eino handler sets nothing but the Go error type there, and the message is a masked attribute.
func (s *maskedSpan) SetStatus(code codes.Code, description string) {
	if code != codes.Error {
		s.Span.SetStatus(code, description)
		return
	}
	raw := captureString(description, s.m.cfg)
	s.reg.update(s.Span.SpanContext(), func(r *spanRecord) { r.status = &rawStatus{code: code, description: raw} })
	s.Span.SetStatus(code, s.m.text(raw))
}

// SetName gives a rename the treatment the name given at start gets.
func (s *maskedSpan) SetName(name string) {
	raw := captureString(name, s.m.cfg)
	s.keep(nil, nil, &raw)
	s.Span.SetName(s.m.text(raw))
}

// AddLink gives a late link the treatment the links given at start get.
func (s *maskedSpan) AddLink(link trace.Link) { s.addLink(link) }

func (s *maskedSpan) addLink(link trace.Link) {
	raw := s.m.captured(link.Attributes)
	// The SDK refuses a link that is invalid, has no attributes and no trace state: it sees the attributes after masking.
	if !link.SpanContext.IsValid() && len(raw) == 0 && link.SpanContext.TraceState().Len() == 0 {
		return
	}
	raw = limitAttributes(raw, s.caps.linkAttrs)
	s.reg.update(s.Span.SpanContext(), func(r *spanRecord) {
		if r.ending {
			return
		}
		r.links = append(r.links, rawLink{sc: link.SpanContext, attrs: raw})
		if over := len(r.links) - s.caps.links; s.caps.links >= 0 && over > 0 {
			r.links = append([]rawLink(nil), r.links[over:]...)
			r.linksDropped += over
		}
	})
}

// End replaces the credentials known by now in what the span carries, adds its events and links, and ends it.
func (s *maskedSpan) End(opts ...trace.SpanEndOption) {
	if s.Span.IsRecording() {
		raw := s.reg.rawSnapshot(s.Span.SpanContext())
		if current, ok := s.Span.(sdktrace.ReadOnlySpan); ok {
			var changed []attribute.KeyValue
			for _, kv := range current.Attributes() {
				v := kv.Value
				if r, ok := raw.attrs[kv.Key]; ok {
					v = r
				}
				if rep := s.m.replaced([]attribute.KeyValue{{Key: kv.Key, Value: v}})[0]; rep.Value.Emit() != kv.Value.Emit() {
					changed = append(changed, rep)
				}
			}
			if len(changed) > 0 {
				s.Span.SetAttributes(changed...)
			}
			name := current.Name()
			if raw.name != nil {
				name = *raw.name
			}
			if rep := s.m.text(name); rep != current.Name() {
				s.Span.SetName(rep)
			}
		}
		// What the buffer let go of is counted by the SDK as it would have been: placeholders, all evicted by what follows.
		for i := 0; i < raw.linksDropped; i++ {
			s.Span.AddLink(trace.Link{Attributes: []attribute.KeyValue{attribute.Bool("dropped", true)}})
		}
		for i := 0; i < raw.eventsDropped; i++ {
			s.Span.AddEvent("")
		}
		for _, l := range raw.links {
			s.Span.AddLink(trace.Link{SpanContext: l.sc, Attributes: s.m.replaced(l.attrs)})
		}
		for _, e := range raw.events {
			attrs := s.m.replaced(e.attrs)
			for i, kv := range attrs {
				if kv.Key == "exception.stacktrace" && kv.Value.Type() == attribute.STRING {
					attrs[i].Value = attribute.StringValue(capStack(kv.Value.AsString()))
				}
			}
			s.Span.AddEvent(s.m.text(e.name), trace.WithAttributes(attrs...), trace.WithTimestamp(e.time))
		}
		if raw.status != nil {
			s.Span.SetStatus(raw.status.code, s.m.text(raw.status.description))
		}
		// Restoration is 1:1: the record keeps exactly the newest events and links the SDK kept of the snapshot this End
		// replayed. The record stopped taking additions when the snapshot was taken, so it is that snapshot.
		if current, ok := s.Span.(sdktrace.ReadOnlySpan); ok {
			keptEvents, keptLinks := len(current.Events()), len(current.Links())
			if keptEvents < len(raw.events) {
				raw.events = raw.events[len(raw.events)-keptEvents:]
			}
			if keptLinks < len(raw.links) {
				raw.links = raw.links[len(raw.links)-keptLinks:]
			}
			s.reg.update(s.Span.SpanContext(), func(r *spanRecord) { r.events, r.links = raw.events, raw.links })
		}
	}
	s.Span.End(opts...)
}
