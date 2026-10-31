package auto

import (
	"context"
	"math"
	"sort"
	"strings"
	"sync"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/instrumentation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

type spanIdentity struct {
	trace trace.TraceID
	span  trace.SpanID
}

func identity(span trace.SpanContext) spanIdentity {
	return spanIdentity{span.TraceID(), span.SpanID()}
}

type spanRecord struct {
	span    sdktrace.ReadOnlySpan
	parent  spanIdentity
	covered bool
}

// Live dedup ancestry is process-local and removed at OnEnd. Credentials are
// deliberately independent: there is no credential inheritance at span start.
// Coverage is inherited at OnStart, so native intermediate spans do not create
// duplicate wire spans, even if an ancestor ends before its child.
type spanPrivacy struct {
	mu    sync.Mutex
	spans map[spanIdentity]*spanRecord
}

func (p *spanPrivacy) clear() {
	p.mu.Lock()
	p.spans = nil
	p.mu.Unlock()
}

func modelSpan(span sdktrace.ReadOnlySpan) bool {
	if span != nil {
		for _, kv := range span.Attributes() {
			if kv.Key == "gen_ai.span.kind" {
				switch kv.Value.AsString() {
				case "LLM", "EMBEDDING", "RERANKER", "GUARDRAIL":
					return true
				}
			}
		}
	}
	return false
}

// Called with the store locked. Recheck live attributes as middleware may set
// its model kind after Start. No user stringification or context traversal.
func (p *spanPrivacy) covered(key spanIdentity) bool {
	for depth := 0; depth < 256; depth++ {
		r := p.spans[key]
		if r == nil {
			return false
		}
		if r.covered || modelSpan(r.span) {
			r.covered = true
			return true
		}
		key = r.parent
	}
	return false
}

func (p *processorRegistration) startPrivacy(ctx context.Context, span sdktrace.ReadWriteSpan) {
	store := p.privacy
	store.mu.Lock()
	defer store.mu.Unlock()
	if !p.active.Load() {
		return
	}
	if store.spans == nil {
		store.spans = make(map[spanIdentity]*spanRecord)
	}
	parent := identity(span.Parent())
	store.spans[identity(span.SpanContext())] = &spanRecord{span: span, parent: parent, covered: modelSpan(span) || store.covered(parent)}
}
func (p *processorRegistration) covered(ctx context.Context) bool {
	store := p.privacy
	store.mu.Lock()
	defer store.mu.Unlock()
	return store.covered(identity(trace.SpanFromContext(ctx).SpanContext()))
}
func (p *processorRegistration) endPrivacy(span sdktrace.ReadOnlySpan) {
	store := p.privacy
	store.mu.Lock()
	defer store.mu.Unlock()
	delete(store.spans, identity(span.SpanContext()))
}

// Never mutate an SDK snapshot: other application exporters see their original
// span. Override every metadata surface, including all tracestate locations.
type privateSpan struct {
	sdktrace.ReadOnlySpan
	name        string
	attrs       []attribute.KeyValue
	events      []sdktrace.Event
	links       []sdktrace.Link
	status      sdktrace.Status
	res         *resource.Resource
	scope       instrumentation.Scope
	ctx, parent trace.SpanContext
}

func (s privateSpan) Name() string                                    { return s.name }
func (s privateSpan) Attributes() []attribute.KeyValue                { return s.attrs }
func (s privateSpan) Events() []sdktrace.Event                        { return s.events }
func (s privateSpan) Links() []sdktrace.Link                          { return s.links }
func (s privateSpan) Status() sdktrace.Status                         { return s.status }
func (s privateSpan) Resource() *resource.Resource                    { return s.res }
func (s privateSpan) InstrumentationScope() instrumentation.Scope     { return s.scope }
func (s privateSpan) InstrumentationLibrary() instrumentation.Library { return s.scope }
func (s privateSpan) SpanContext() trace.SpanContext                  { return s.ctx }
func (s privateSpan) Parent() trace.SpanContext                       { return s.parent }

func hidesContent(c TraceConfig) bool {
	return c.HideInputs || c.HideOutputs || c.HideInputText || c.HideOutputText || c.HideInputMessages || c.HideOutputMessages
}

// A single bounded walk covers attributes of every OTel type and every surface.
// OTel values are immutable typed trees (unknown application objects cannot be
// stored in them). Avoid Emit/AsInterface/JSON marshaling unknown values.
type spanSanitizer struct {
	config       Config
	operation    string
	secrets      *secretSnapshot
	nodes, bytes int
	// siblings are extra MIME/type hints for one attribute object (capture
	// time: wire hint keys for context attributes). Nil at export.
	siblings map[string]any
}

func (s *spanSanitizer) spend(n, bytes, depth int) {
	s.nodes -= n
	s.bytes -= bytes
	if depth > 32 || s.nodes < 0 || s.bytes < 0 {
		panic("traceai privacy budget") // Caught by exportSpan; never export raw fallback.
	}
}
func (s *spanSanitizer) text(key, value string) string {
	s.spend(1, len(value), 0)
	value = wire.PrivacyAttributeStringWithMasker(key, value, s.secrets.replace)
	return sanitize(value, s.config.TraceConfig).(string)
}
func (s *spanSanitizer) attrs(attrs []attribute.KeyValue, event bool, depth int, _ int) []attribute.KeyValue {
	s.spend(len(attrs), 0, depth)
	// One object preserves all sibling hints, including string-array/indexed MIME
	// hints. The catalog walker is shared with JSON bodies, not reimplemented here.
	values := make(map[string]any, len(attrs))
	original := make(map[string]attribute.Value, len(attrs))
	for _, kv := range attrs {
		key := string(kv.Key)
		s.spend(0, len(key), depth)
		if event && hidesContent(s.config.TraceConfig) && key != "exception.type" {
			kv.Value = attribute.StringValue(RedactedValue)
		}
		kv, keep := s.config.TraceConfig.mask(kv, s.operation)
		if !keep {
			continue
		}
		original[key] = kv.Value
		values[key] = s.native(kv.Value, depth+1)
	}
	clean := wire.PrivacyValueWithMasker(values, "", s.siblings, s.secrets.replace)
	// Hide references with the same detached sibling context as the catalog
	// walk. Per-value hiding cannot associate flattened MIME/type hints with URLs.
	hidden := sanitize(clean, s.config.TraceConfig)
	if hidden == RedactedValue {
		// The attribute object itself was an image block (or exceeded the hide
		// policy's own budget): all content is hidden. Protected identity,
		// outcome and usage keep their CATALOG-WALKED form (scrubbed with every
		// sibling hint), never the unchecked original, then pass the hide policy
		// alone. Callers must not restore omitted keys from their input.
		hidden = s.protectedOnly(clean, attrs, original)
	}
	fields, ok := hidden.(map[string]any)
	if !ok {
		panic("traceai privacy attribute object")
	}
	out := make([]attribute.KeyValue, 0, len(attrs))
	for _, kv := range attrs {
		key := string(kv.Key)
		if value, ok := fields[key]; ok {
			// The host hide-policy marker is not raw base64. Keep that stronger
			// policy's stable result even if its spelling resembles media magic.
			if old := original[key]; old.Type() == attribute.STRING && old.AsString() == RedactedValue {
				value = RedactedValue
			}
			out = append(out, attribute.KeyValue{Key: attribute.Key(s.text("", key)), Value: s.typed(value, original[key], depth+1)})
			delete(fields, key)
		}
	}
	return out
}

// protectedOnly selects the A7 protected keys from a catalog-walked attribute
// object whose hide-policy result was the whole-object replacement. Each value
// is hidden on its own (keyed, so path-based image policy still applies); a
// value the policy cannot check is dropped.
func (s *spanSanitizer) protectedOnly(clean any, attrs []attribute.KeyValue, original map[string]attribute.Value) map[string]any {
	fields, _ := clean.(map[string]any)
	out := map[string]any{}
	for _, kv := range attrs {
		key := string(kv.Key)
		value, present := fields[key]
		old, kept := original[key]
		if !present || !kept || protectedAttributePriority(attribute.KeyValue{Key: kv.Key, Value: old}) < 0 {
			continue
		}
		if one, ok := sanitize(map[string]any{key: value}, s.config.TraceConfig).(map[string]any); ok {
			if value, ok := one[key]; ok {
				out[key] = value
			}
		}
	}
	return out
}

// Convert only immutable OTel types; never call Emit, AsInterface, String or a
// user marshaler. Both conversion directions share the span's fail-closed budget.
func (s *spanSanitizer) native(v attribute.Value, depth int) any {
	s.spend(1, 0, depth)
	switch v.Type() {
	case attribute.STRING:
		text := v.AsString()
		s.spend(0, len(text), depth)
		return text
	case attribute.BYTESLICE:
		return "<bytes omitted>"
	case attribute.FLOAT64:
		f := privateFloat(v.AsFloat64())
		if f.Type() == attribute.STRING {
			return f.AsString()
		}
		return f.AsFloat64()
	case attribute.BOOL:
		return v.AsBool()
	case attribute.INT64:
		return v.AsInt64()
	case attribute.EMPTY:
		return nil
	case attribute.MAP:
		fields := v.AsMap()
		s.spend(len(fields), 0, depth)
		out := make(map[string]any, len(fields))
		for _, field := range fields {
			s.spend(0, len(field.Key), depth)
			out[string(field.Key)] = s.native(field.Value, depth+1)
		}
		return out
	case attribute.STRINGSLICE:
		values := v.AsStringSlice()
		s.spend(len(values), 0, depth)
		out := make([]any, len(values))
		for i, value := range values {
			s.spend(0, len(value), depth)
			out[i] = value
		}
		return out
	case attribute.FLOAT64SLICE:
		values := v.AsFloat64Slice()
		s.spend(len(values), 0, depth)
		out := make([]any, len(values))
		for i, value := range values {
			out[i] = s.native(attribute.Float64Value(value), depth+1)
		}
		return out
	case attribute.BOOLSLICE:
		values := v.AsBoolSlice()
		s.spend(len(values), 0, depth)
		out := make([]any, len(values))
		for i, value := range values {
			out[i] = value
		}
		return out
	case attribute.INT64SLICE:
		values := v.AsInt64Slice()
		s.spend(len(values), 0, depth)
		out := make([]any, len(values))
		for i, value := range values {
			out[i] = value
		}
		return out
	case attribute.SLICE:
		values := v.AsSlice()
		s.spend(len(values), 0, depth)
		out := make([]any, len(values))
		for i, value := range values {
			out[i] = s.native(value, depth+1)
		}
		return out
	default:
		return "attribute.Value"
	}
}
func (s *spanSanitizer) typed(value any, original attribute.Value, depth int) attribute.Value {
	s.spend(1, 0, depth)
	switch x := value.(type) {
	case nil:
		return attribute.Value{}
	case string:
		return attribute.StringValue(x)
	case bool:
		return attribute.BoolValue(x)
	case int64:
		return attribute.Int64Value(x)
	case float64:
		return privateFloat(x)
	case map[string]any:
		old := map[string]attribute.Value{}
		if original.Type() == attribute.MAP {
			for _, field := range original.AsMap() {
				old[string(field.Key)] = field.Value
			}
		}
		keys := make([]string, 0, len(x))
		for key := range x {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		out := make([]attribute.KeyValue, 0, len(x))
		for _, key := range keys {
			out = append(out, attribute.KeyValue{Key: attribute.Key(s.text("", key)), Value: s.typed(x[key], old[key], depth+1)})
		}
		return attribute.MapValue(out...)
	case []any:
		out := make([]attribute.Value, len(x))
		for i, item := range x {
			out[i] = s.typed(item, attribute.Value{}, depth+1)
		}
		switch original.Type() {
		case attribute.STRINGSLICE:
			values := make([]string, len(x))
			for i, item := range out {
				if item.Type() != attribute.STRING {
					return attribute.SliceValue(out...)
				}
				values[i] = item.AsString()
			}
			return attribute.StringSliceValue(values)
		case attribute.INT64SLICE:
			values := make([]int64, len(x))
			for i, item := range out {
				if item.Type() != attribute.INT64 {
					return attribute.SliceValue(out...)
				}
				values[i] = item.AsInt64()
			}
			return attribute.Int64SliceValue(values)
		case attribute.BOOLSLICE:
			values := make([]bool, len(x))
			for i, item := range out {
				if item.Type() != attribute.BOOL {
					return attribute.SliceValue(out...)
				}
				values[i] = item.AsBool()
			}
			return attribute.BoolSliceValue(values)
		case attribute.FLOAT64SLICE:
			values := make([]float64, len(x))
			for i, item := range out {
				if item.Type() != attribute.FLOAT64 {
					return attribute.SliceValue(out...)
				}
				values[i] = item.AsFloat64()
			}
			return attribute.Float64SliceValue(values)
		}
		return attribute.SliceValue(out...)
	default:
		return attribute.StringValue("attribute.Value")
	}
}
func privateFloat(v float64) attribute.Value {
	switch {
	case math.IsNaN(v):
		return attribute.StringValue("NaN")
	case math.IsInf(v, 1):
		return attribute.StringValue("Infinity")
	case math.IsInf(v, -1):
		return attribute.StringValue("-Infinity")
	default:
		return attribute.Float64Value(v)
	}
}
func (s *spanSanitizer) spanContext(ctx trace.SpanContext) trace.SpanContext {
	state := trace.TraceState{}
	ctx.TraceState().Walk(func(key, value string) bool {
		var err error
		state, err = state.Insert(s.text("", key), s.text(key, value))
		if err != nil {
			state = state.Delete(key)
		}
		return true
	})
	return ctx.WithTraceState(state)
}

// exportResource builds the FI-owned resource view (A6). Every application
// resource attribute is kept exactly, including service.name; FI routing
// (project_*) is overlaid; other agent defaults only fill absent keys. The
// span's own resource (other exporters' view) is never mutated.
func exportResource(app, agent *resource.Resource) *resource.Resource {
	values := map[attribute.Key]attribute.KeyValue{}
	schema := ""
	if app != nil {
		schema = app.SchemaURL()
		for _, kv := range app.Attributes() {
			values[kv.Key] = kv
		}
	}
	if agent != nil {
		for _, kv := range agent.Attributes() {
			if _, exists := values[kv.Key]; !exists || strings.HasPrefix(string(kv.Key), "project_") {
				values[kv.Key] = kv
			}
		}
	}
	attrs := make([]attribute.KeyValue, 0, len(values))
	for _, kv := range values {
		attrs = append(attrs, kv)
	}
	return resource.NewWithAttributes(schema, attrs...)
}

func exportSpan(span sdktrace.ReadOnlySpan, c Config, res *resource.Resource) (out sdktrace.ReadOnlySpan) {
	// On sanitizer errors retain identity/timing/status code but no unchecked
	// content, resources, scopes, event names, link attributes or tracestate.
	fallback := privateSpan{ReadOnlySpan: span, name: "traceai span", status: sdktrace.Status{Code: span.Status().Code}, res: resource.Empty(), ctx: span.SpanContext().WithTraceState(trace.TraceState{}), parent: span.Parent().WithTraceState(trace.TraceState{})}
	out = fallback
	defer func() {
		if recover() != nil {
			out = fallback
			warn()
		}
	}()
	s := &spanSanitizer{config: c, secrets: processSecrets.snapshot(), nodes: 16384, bytes: 32 * 1024 * 1024}
	for _, kv := range span.Attributes() {
		if kv.Key == "gen_ai.operation.name" {
			s.operation = kv.Value.AsString()
		}
	}
	status := span.Status()
	status.Description = s.text("", status.Description)
	if hidesContent(c.TraceConfig) {
		status.Description = ""
	}
	events := append([]sdktrace.Event(nil), span.Events()...)
	s.spend(len(events), 0, 0)
	for i := range events {
		events[i].Name = s.text("", events[i].Name)
		if hidesContent(c.TraceConfig) && events[i].Name != "exception" {
			events[i].Name = RedactedValue
		}
		events[i].Attributes = s.attrs(events[i].Attributes, true, 0, 0)
	}
	links := append([]sdktrace.Link(nil), span.Links()...)
	s.spend(len(links), 0, 0)
	for i := range links {
		links[i].Attributes = s.attrs(links[i].Attributes, false, 0, 0)
		links[i].SpanContext = s.spanContext(links[i].SpanContext)
	}
	r := exportResource(span.Resource(), res)
	if r != nil {
		r = resource.NewWithAttributes(s.text("", r.SchemaURL()), s.attrs(r.Attributes(), false, 0, 0)...)
	}
	scope := span.InstrumentationScope()
	scope.Name, scope.Version, scope.SchemaURL = s.text("", scope.Name), s.text("", scope.Version), s.text("", scope.SchemaURL)
	scope.Attributes = attribute.NewSet(s.attrs(scope.Attributes.ToSlice(), false, 0, 0)...)
	return privateSpan{ReadOnlySpan: span, name: s.text("", span.Name()), attrs: s.attrs(span.Attributes(), false, 0, 0), events: events, links: links, status: status, res: r, scope: scope, ctx: s.spanContext(span.SpanContext()), parent: s.spanContext(span.Parent())}
}
