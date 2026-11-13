package auto

import (
	"bytes"
	"context"
	"encoding/base64"
	"image"
	"image/png"
	"io"
	"net/http"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func smallPNG(t *testing.T) string {
	t.Helper()
	var out bytes.Buffer
	if err := png.Encode(&out, image.NewRGBA(image.Rect(0, 0, 1, 1))); err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(out.Bytes())
}

func TestS5RegistryLearnedAfterSpanEnd(t *testing.T) {
	const secret = "S5_POST_END_CREDENTIAL_PRIVATE"
	raw := tracetest.NewSpanRecorder()
	p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(raw))
	defer p.Shutdown(context.Background())
	_, span := p.Tracer("queued").Start(context.Background(), "already-ended", trace.WithAttributes(attribute.String("output.value", secret)))
	span.End()
	// Simulate the immutable SDK snapshot waiting in a batch queue. Discovery
	// happens after OnEnd, but before the exporter serializes that snapshot.
	RegisterSecret(secret)
	sink := tracetest.NewInMemoryExporter()
	exporter := resourceExporter{SpanExporter: sink, res: resource.Empty()}
	if err := exporter.ExportSpans(context.Background(), raw.Ended()); err != nil {
		t.Fatal(err)
	}
	if len(sink.GetSpans()) != 1 || sink.GetSpans()[0].Attributes[0].Value.AsString() != "<redacted>" || spanAttr(raw.Ended()[0], "output.value").AsString() != secret {
		t.Fatal("queued span used an end-time secret snapshot or mutated original")
	}
}

func TestS5CaptureDefersValueMasking(t *testing.T) {
	const prefix = "S5_OVERLAP_DIAGNOSTIC_PREFIX"
	const longer = prefix + "_LATE_REGISTERED_SUFFIX_PRIVATE"
	RegisterSecret(prefix)
	cfg := Config{APIKey: prefix}
	formatted := captureString(longer, cfg)
	if formatted != longer || safeString(prefix, cfg) != "<redacted>" {
		t.Fatal("capture masked values before S1 or diagnostics exposed a known value")
	}
	raw := tracetest.NewSpanRecorder()
	p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(raw))
	defer p.Shutdown(context.Background())
	_, span := p.Tracer("capture").Start(context.Background(), "capture", trace.WithAttributes(attribute.String("output.value", formatted)))
	span.End()
	RegisterSecret(longer)
	view := exportSpan(raw.Ended()[0], cfg, resource.Empty())
	if spanAttr(view, "output.value").AsString() != "<redacted>" {
		t.Fatal("an early prefix replacement prevented later longest-first masking")
	}
}

func TestS5SuppressedObservationNeverBuffersBody(t *testing.T) {
	for _, mode := range []string{"model", "explicit", "nested-capture"} {
		t.Run(mode, func(t *testing.T) {
			ownedRegressionSetup(t)
			rec := tracetest.NewSpanRecorder()
			if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
				t.Fatal(err)
			}
			ctx := context.Background()
			switch mode {
			case "model":
				var parent, native trace.Span
				ctx, parent = agent.current.provider.Tracer("library").Start(ctx, "model", trace.WithAttributes(attribute.String("gen_ai.span.kind", "LLM")))
				ctx, native = agent.current.provider.Tracer("native").Start(ctx, "native")
				defer parent.End()
				defer native.End()
			case "explicit":
				ctx = SuppressInstrumentation(ctx)
			case "nested-capture":
				flight := &captureFlight{}
				flight.active.Store(true)
				ctx = context.WithValue(ctx, captureKey, flight)
			}
			secret := "S5_SUPPRESSED_HEADER_PRIVATE_" + mode
			body := &s5UntouchedBody{t: t}
			req, _ := http.NewRequestWithContext(ctx, "POST", "http://127.0.0.1/v1/chat/completions", body)
			req.GetBody = func() (io.ReadCloser, error) { t.Fatal("suppressed request replay was evaluated"); return nil, nil }
			req.Header.Set("X-Api-Key", secret)
			responseBody := io.NopCloser(strings.NewReader("untouched"))
			original := &http.Response{StatusCode: 200, Body: responseBody}
			base := round4TransportFunc(func(out *http.Request) (*http.Response, error) {
				if out.Body != body || out.GetBody == nil || out.Header.Get("X-Api-Key") != secret {
					t.Fatal("suppressed observation changed body, replay or headers")
				}
				return original, nil
			})
			got, err := Transport(base).RoundTrip(req)
			if err != nil || got != original || got.Body != responseBody || processSecrets.snapshot().replace(secret) != "<redacted>" {
				t.Fatal("suppressed hook failed credential observation or response identity")
			}
			_ = got.Body.Close()
		})
	}
}

type s5UntouchedBody struct{ t *testing.T }

func (b *s5UntouchedBody) Read([]byte) (int, error) {
	b.t.Fatal("suppressed hook read request body")
	return 0, io.EOF
}
func (b *s5UntouchedBody) Close() error { b.t.Fatal("suppressed hook closed request body"); return nil }
