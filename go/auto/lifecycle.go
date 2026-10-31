package auto

import (
	"context"
	"fmt"
	"sync/atomic"

	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Registrations do not own processors. OTel unregisters with Background(); only
// the agent's explicitly bounded Shutdown may shut down a real exporter.
type processorRegistration struct {
	processor sdktrace.SpanProcessor
	active    atomic.Bool
	config    Config
	res       *resource.Resource
	privacy   *spanPrivacy
	queued    bool
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
			p.endPrivacy(span)
			if p.processor == nil {
				return nil // Host exporters receive the original application view.
			}
			if p.queued {
				// The exporter snapshots the process registry after queueing, so a
				// previously ended span can use credentials learned before export.
				p.processor.OnEnd(span)
			} else {
				// A custom processor replaces our exporter: its handoff is the
				// privacy boundary, using exactly the network sanitizer.
				p.processor.OnEnd(exportSpan(span, p.config, p.res))
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
