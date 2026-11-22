package auto

import (
	"context"
	"fmt"
	"sync/atomic"

	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// wireScope is the instrumentation scope of the agent's wire spans.
const wireScope = "github.com/future-agi/traceAI/go/auto"

// Registrations do not own processors. OTel unregisters with Background(); only
// the agent's explicitly bounded Shutdown may shut down a real exporter.
type processorRegistration struct {
	processor sdktrace.SpanProcessor
	active    atomic.Bool
	config    Config
	res       *resource.Resource
	privacy   *spanPrivacy
	queued    bool
	limits    *exportLimits
	// aiOnly is set when the FI exporter sits on an application-owned
	// provider: it then forwards only aiSpan spans.
	aiOnly atomic.Bool
}

// aiSpan reports a span the FI exporter forwards from an application-owned
// provider: the agent's own wire spans, and spans that carry gen_ai.span.kind
// or fi.span.kind (traceAI modules). Every other application span still
// reaches the application's own exporters, never Future AGI.
func aiSpan(span sdktrace.ReadOnlySpan) bool {
	if span.InstrumentationScope().Name == wireScope {
		return true
	}
	for _, kv := range span.Attributes() {
		if kv.Key == "gen_ai.span.kind" || kv.Key == "fi.span.kind" {
			return true
		}
	}
	return false
}

func (p *processorRegistration) OnStart(ctx context.Context, span sdktrace.ReadWriteSpan) {
	if p.active.Load() {
		_ = guarded(func() error {
			p.startPrivacy(ctx, span)
			if p.processor != nil {
				p.processor.OnStart(ctx, span)
			}
			return nil
		})
	}
}
func (p *processorRegistration) OnEnd(span sdktrace.ReadOnlySpan) {
	if p.active.Load() {
		_ = guarded(func() error {
			session, raw := p.endPrivacy(span)
			if p.processor == nil {
				return nil // Host exporters receive the original application view.
			}
			if p.aiOnly.Load() && !aiSpan(span) {
				return nil
			}
			if raw != nil {
				span = raw.with(span)
			}
			if session != nil {
				// The export sanitizer runs after the span's record is gone.
				span = sessionSpan{ReadOnlySpan: span, session: *session}
			}
			if p.queued {
				// The exporter snapshots the process registry after queueing, so a
				// previously ended span can use credentials learned before export.
				p.processor.OnEnd(span)
			} else {
				// A custom processor replaces our exporter: its handoff is the
				// privacy boundary, using exactly the network sanitizer.
				p.processor.OnEnd(exportSpanLimited(span, p.config, p.res, p.limits.value()))
			}
			return nil
		})
	}
}
func (p *processorRegistration) Shutdown(context.Context) error { return nil }
func (p *processorRegistration) ForceFlush(ctx context.Context) error {
	if p.active.Load() && p.processor != nil {
		return guarded(func() error { return p.processor.ForceFlush(ctx) })
	}
	return nil
}

func guarded(f func() error) (err error) {
	defer func() {
		if recover() != nil {
			warn()
			err = fmt.Errorf("traceai auto callback failed")
		}
	}()
	return f()
}
