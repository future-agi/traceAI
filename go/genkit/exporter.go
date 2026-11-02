// Package traceaigenkit supplies an export-time privacy boundary for Genkit's
// native OpenTelemetry spans. It does not start spans or replace instrumentation.
package traceaigenkit

import (
	"context"
	"os"
	"strings"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Config controls which native Genkit span fields may leave the process.
// NoContent and CaptureActionIO=false should still be configured on Genkit's
// native instrumentor; this exporter protects independently against error
// descriptions and accidental opt-in to SpanOnly model messages.
type Config struct {
	HideInputs       bool
	HideOutputs      bool
	HideErrorDetails bool
}

// ConfigFromEnv reads the privacy switches once at provider construction.
func ConfigFromEnv() Config {
	enabled := func(name string) bool { return strings.EqualFold(strings.TrimSpace(os.Getenv(name)), "true") }
	return Config{HideInputs: enabled("FI_HIDE_INPUTS"), HideOutputs: enabled("FI_HIDE_OUTPUTS"), HideErrorDetails: enabled("FI_HIDE_ERROR_DETAILS")}
}

type redactingExporter struct {
	downstream sdktrace.SpanExporter
	config     Config
}

// NewExporter wraps a single application-owned exporter. Pass the result to
// WithBatcher or WithSyncer, never alongside the unwrapped exporter: doing so
// would export duplicates, including unredacted spans.
func NewExporter(downstream sdktrace.SpanExporter, config Config) sdktrace.SpanExporter {
	return &redactingExporter{downstream: downstream, config: config}
}

func (e *redactingExporter) ExportSpans(ctx context.Context, spans []sdktrace.ReadOnlySpan) error {
	clean := make([]sdktrace.ReadOnlySpan, len(spans))
	for i, span := range spans {
		copySpan := &redactedSpan{ReadOnlySpan: span}
		copySpan.attributes = make([]attribute.KeyValue, 0, len(span.Attributes()))
		for _, kv := range span.Attributes() {
			key := string(kv.Key)
			switch {
			case e.config.HideInputs && (strings.HasPrefix(key, "gen_ai.input.") || key == "gen_ai.system_instructions" || key == "genkit.input" || key == "input.value"):
				continue
			case e.config.HideOutputs && (strings.HasPrefix(key, "gen_ai.output.") || key == "genkit.output" || key == "output.value"):
				continue
			case e.config.HideErrorDetails && (key == "error.message" || strings.HasPrefix(key, "exception.")):
				continue
			}
			copySpan.attributes = append(copySpan.attributes, kv)
		}
		copySpan.status = span.Status()
		if e.config.HideErrorDetails {
			copySpan.status.Description = ""
		}
		copySpan.events = make([]sdktrace.Event, len(span.Events()))
		for j, event := range span.Events() {
			copySpan.events[j] = event
			copySpan.events[j].Attributes = make([]attribute.KeyValue, 0, len(event.Attributes))
			for _, kv := range event.Attributes {
				key := string(kv.Key)
				if e.config.HideErrorDetails && (strings.HasPrefix(key, "exception.") || key == "error.message") {
					continue
				}
				copySpan.events[j].Attributes = append(copySpan.events[j].Attributes, kv)
			}
		}
		clean[i] = copySpan
	}
	return e.downstream.ExportSpans(ctx, clean)
}

func (e *redactingExporter) Shutdown(ctx context.Context) error { return e.downstream.Shutdown(ctx) }

// Embedding the original ReadOnlySpan preserves its SDK-private method and
// trace identity, timing, links, resource, and all unrelated SDK metadata.
type redactedSpan struct {
	sdktrace.ReadOnlySpan
	attributes []attribute.KeyValue
	events     []sdktrace.Event
	status     sdktrace.Status
}

func (s *redactedSpan) Attributes() []attribute.KeyValue { return s.attributes }
func (s *redactedSpan) Events() []sdktrace.Event         { return s.events }
func (s *redactedSpan) Status() sdktrace.Status          { return s.status }
