package auto

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	"google.golang.org/protobuf/proto"
)

// R6A verifier BLOCKING B2 (2026-10-31): the joint capture-time walk scrubbed
// protected-key media correctly, but two fallbacks then restored the ORIGINAL
// protected attributes without their MIME siblings: (1) the hide-images policy
// classified the whole context object as an image block (root type=image_url),
// so the walk returned nothing; (2) an unrelated large attribute exhausted the
// privacy budget. The export sanitizer then saw headerless PCM (a map under
// project_name, or integer samples under numeric gen_ai.usage.*) without its
// MIME context and exported it raw.

const r6aSampleBase = 7_340_000 // Distinctive raw sample values.

type r6aFallbackCase struct {
	name     string
	numeric  bool // R2: integer samples under numeric gen_ai.usage.* keys.
	mode     string
	hideImgs bool
}

func r6aFallbackContext(numeric bool, mode string) []attribute.KeyValue {
	var extra []attribute.KeyValue
	if numeric {
		extra = append(extra, attribute.String("gen_ai.usage.extension.mime_type", "audio/L16"))
		for i := 0; i < 20; i++ {
			extra = append(extra, attribute.Int64(fmt.Sprintf("gen_ai.usage.extension.data.%03d", i), int64(r6aSampleBase+i)))
		}
	} else {
		extra = append(extra, attribute.Map("project_name", attribute.String("data", r6aPCM)), attribute.String("project_name.mime_type", "audio/L16"))
	}
	switch mode {
	case "image-object":
		extra = append(extra, attribute.String("type", "image_url"), attribute.String("image_url", "https://example.invalid/r6a.png"))
	case "budget":
		big := make([]int64, 17000)
		for i := range big {
			big[i] = int64(i)
		}
		extra = append(extra, attribute.Int64Slice("app.unrelated.samples", big))
	}
	return extra
}

func r6aRawSamples(attrs []attribute.KeyValue) int {
	raw := 0
	for _, kv := range attrs {
		if strings.HasPrefix(string(kv.Key), "gen_ai.usage.extension.data") && kv.Value.Type() == attribute.INT64 && kv.Value.AsInt64() >= r6aSampleBase {
			raw++
		}
	}
	return raw
}

func r6aJointSessionID(t *testing.T) string {
	t.Helper()
	for _, kv := range captureContext([]attribute.KeyValue{attribute.String("session.id", "session-1")}, nil, Config{}, "chat") {
		if kv.Key == "session.id" {
			return kv.Value.AsString()
		}
	}
	t.Fatal("joint walk lost session.id")
	return ""
}

// Unit level: neither fallback restores an unchecked protected value.
func TestR6aCaptureContextFallbackNeverRestoresRaw(t *testing.T) {
	for _, numeric := range []bool{false, true} {
		for _, mode := range []string{"normal", "image-object", "budget"} {
			t.Run(fmt.Sprintf("numeric=%v/%s", numeric, mode), func(t *testing.T) {
				extra := append(r6aFallbackContext(numeric, mode), attribute.String("user.id", "user-1"), attribute.String("session.id", "session-1"))
				out := captureContext(extra, nil, Config{TraceConfig: TraceConfig{HideInputImages: true}}, "chat")
				got := map[string]attribute.Value{}
				for _, kv := range out {
					got[string(kv.Key)] = kv.Value
					if kv.Value.Type() == attribute.MAP || kv.Value.Type() == attribute.STRING {
						if strings.Contains(kv.Value.Emit(), r6aPCM) {
							t.Fatalf("raw PCM restored through %s", kv.Key)
						}
						if strings.Contains(kv.Value.Emit(), "example.invalid") {
							t.Fatalf("hidden image reference restored through %s", kv.Key)
						}
					}
				}
				if n := r6aRawSamples(out); n != 0 {
					t.Fatalf("%d raw integer samples restored", n)
				}
				// session.id is credential-marked by the shared catalog; every path
				// must yield the joint walk's form, never the unchecked original.
				if got["user.id"].AsString() != "user-1" || got["session.id"].AsString() != r6aJointSessionID(t) {
					t.Fatalf("protected identity lost or unchecked: %v", out)
				}
				degraded := got["traceai.wire.privacy_degraded"]
				if (mode == "budget") != (degraded.Type() == attribute.BOOL && degraded.AsBool()) {
					t.Fatalf("degradation marker mismatch in %s mode: %v", mode, out)
				}
				if mode == "budget" {
					if _, ok := got["app.unrelated.samples"]; ok {
						t.Fatal("budget failure kept unchecked content context")
					}
				}
				if !numeric && mode != "budget" {
					if v, ok := got["project_name"]; !ok || v.Type() != attribute.MAP {
						t.Fatalf("protected project_name lost or retyped: %v", out)
					}
				}
			})
		}
	}
}

// Second-tier failure: a protected value that itself exceeds the privacy
// budget is dropped (never restored raw); scalar identity is still kept.
func TestR6aCaptureContextOversizedProtectedFailsClosed(t *testing.T) {
	fields := make([]attribute.KeyValue, 17000)
	for i := range fields {
		fields[i] = attribute.String(fmt.Sprintf("f%05d", i), r6aPCM)
	}
	extra := []attribute.KeyValue{attribute.Map("project_name", fields...), attribute.String("project_name.mime_type", "audio/L16"), attribute.String("user.id", "user-1")}
	out := captureContext(extra, nil, Config{}, "chat")
	got := map[string]attribute.Value{}
	for _, kv := range out {
		got[string(kv.Key)] = kv.Value
		if strings.Contains(kv.Value.Emit(), r6aPCM) {
			t.Fatalf("raw PCM restored through %s", kv.Key)
		}
	}
	if _, ok := got["project_name"]; ok {
		t.Fatal("oversized protected value restored after two budget failures")
	}
	if got["user.id"].AsString() != "user-1" || !got["traceai.wire.privacy_degraded"].AsBool() {
		t.Fatalf("scalar identity or degradation marker lost: %v", out)
	}
	// Hints that alone exceed the budget: nothing checkable remains but the marker.
	hints := make([]string, 17000)
	for i := range hints {
		hints[i] = "audio/L16"
	}
	out = captureContext([]attribute.KeyValue{attribute.String("session.id", r6aPCM), attribute.StringSlice("mime_type", hints)}, nil, Config{}, "chat")
	for _, kv := range out {
		if strings.Contains(kv.Value.Emit(), r6aPCM) {
			t.Fatalf("raw PCM restored through %s", kv.Key)
		}
	}
}

// Export level: an attribute object classified as one image block keeps its
// protected identity in catalog-walked form, never the unchecked original.
func TestR6aExportImageObjectKeepsScrubbedProtected(t *testing.T) {
	s := &spanSanitizer{config: Config{TraceConfig: TraceConfig{HideInputImages: true}}, secrets: noSecrets, nodes: 16384, bytes: 32 * 1024 * 1024}
	out := s.attrs([]attribute.KeyValue{attribute.String("type", "image_url"), attribute.String("image_url", "https://example.invalid/x.png"), attribute.String("mime_type", "audio/L16"), attribute.String("session.id", r6aPCM), attribute.String("user.id", "user-1"), attribute.String("gen_ai.span.kind", "LLM")}, false, 0, 0)
	got := map[string]string{}
	for _, kv := range out {
		got[string(kv.Key)] = kv.Value.Emit()
	}
	if got["user.id"] != "user-1" || got["gen_ai.span.kind"] != "LLM" {
		t.Fatalf("protected identity lost from an image-object attribute set: %v", got)
	}
	if strings.Contains(fmt.Sprint(got), r6aPCM) || strings.Contains(fmt.Sprint(got), "example.invalid") || got["session.id"] == "" {
		t.Fatalf("unchecked content survived the image-object fallback: %v", got)
	}
	if _, ok := got["mime_type"]; ok {
		t.Fatalf("hidden image-object content survived: %v", got)
	}
}

// One-line init, real loopback HTTP, collector-received OTLP at the default
// 128 limit (and a small one): no fallback exports catalog-recognized media.
func TestR6aProtectedFallbackMediaOTLP(t *testing.T) {
	for _, numeric := range []bool{false, true} {
		for _, mode := range []string{"normal", "image-object", "budget"} {
			for _, limit := range []int{32, 128} {
				t.Run(fmt.Sprintf("numeric=%v/%s/limit=%d", numeric, mode, limit), func(t *testing.T) {
					exporterRegressionSetup(t)
					received := privacyCollector(t)
					limits := sdktrace.NewSpanLimits()
					limits.AttributeCountLimit = limit
					host := tracetest.NewSpanRecorder()
					p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(limits), sdktrace.WithSpanProcessor(host))
					defer p.Shutdown(context.Background())
					if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("R6A_FALLBACK_FI_KEY", "R6A_FALLBACK_FI_SECRET"), WithTraceConfig(TraceConfig{HideInputImages: true})); err != nil {
						t.Fatal(err)
					}
					ctx := ContextWithUserID(ContextWithSessionID(context.Background(), "session-1"), "user-1")
					r6aLongChat(t, ContextWithAttributes(ctx, r6aFallbackContext(numeric, mode)...), 80)
					request := flushedPrivacyExport(t, received)
					wire, _ := proto.Marshal(request)
					if bytes.Contains(wire, []byte(r6aPCM)) {
						t.Fatal("raw PCM reached the OTLP export")
					}
					if bytes.Contains(wire, []byte("example.invalid")) {
						t.Fatal("hidden image reference reached the OTLP export")
					}
					if len(host.Ended()) != 1 {
						t.Fatal("host provider lost the wire span")
					}
					if n := r6aRawSamples(host.Ended()[0].Attributes()); n != 0 {
						t.Fatalf("%d raw integer samples reached the host view", n)
					}
					for _, kv := range host.Ended()[0].Attributes() {
						if strings.Contains(kv.Value.Emit(), r6aPCM) {
							t.Fatalf("raw PCM reached the host view through %s", kv.Key)
						}
					}
					attrs := s6ProtoAttrs(request.ResourceSpans[0].ScopeSpans[0].Spans[0])
					for key, value := range attrs {
						if v, ok := value.(*commonpb.AnyValue); ok && strings.HasPrefix(key, "gen_ai.usage.extension.data") {
							if _, raw := v.Value.(*commonpb.AnyValue_IntValue); raw {
								t.Fatalf("raw integer sample %s reached the OTLP export", key)
							}
						}
					}
					for _, key := range []string{"user.id", "session.id", "gen_ai.span.kind", "output.value", "gen_ai.usage.total_tokens"} {
						if attrs[key] == nil {
							t.Errorf("lost %s", key)
						}
					}
					if mode == "budget" && attrs["traceai.wire.privacy_degraded"] == nil {
						t.Error("budget fallback lost its degradation marker")
					}
					if mode == "normal" && !numeric && attrs["project_name.mime_type"] == nil {
						t.Error("control: MIME context separated from its payload")
					}
				})
			}
		}
	}
}
