package auto

import (
	"context"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

const ratioPrivacyImageURL = "https://fixture.invalid/picture.png"

// Obtain the owned provider through the public OnStart API, without replacing
// its sampler or reaching into agent state. OnStart sees the sampler's raw
// attribute; OnEnd receives the production export sanitizer's private view.
type ratioPrivacyProcessor struct {
	*tracetest.SpanRecorder
	provider trace.TracerProvider
	raw      []attribute.KeyValue
}

func (p *ratioPrivacyProcessor) OnStart(ctx context.Context, span sdktrace.ReadWriteSpan) {
	p.provider = span.TracerProvider()
	if span.Name() == "ratio-image" {
		span.SetAttributes(
			attribute.String("type", "image_url"),
			attribute.String("url", ratioPrivacyImageURL),
			attribute.String("app.unrelated", "hidden-with-image-object"),
		)
		p.raw = append([]attribute.KeyValue(nil), span.Attributes()...)
	}
	p.SpanRecorder.OnStart(ctx, span)
}

type ratioPrivacyTransport struct{}

func (ratioPrivacyTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.Body != nil {
		_, _ = io.Copy(io.Discard, req.Body)
		_ = req.Body.Close()
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"id":"ratio-fixture","model":"fixture-model","choices":[{"message":{"role":"assistant","content":"answer"}}],"usage":{"prompt_tokens":100,"completion_tokens":1,"total_tokens":101}}`)),
		Request:    req,
	}, nil
}

func ratioPrivacyValues(attrs []attribute.KeyValue) map[string]attribute.Value {
	out := make(map[string]attribute.Value, len(attrs))
	for _, kv := range attrs {
		out[string(kv.Key)] = kv.Value
	}
	return out
}

// An in-memory wire request exposes the actual owned provider to a public
// custom processor. Neither the request URL nor the image URL is fetched.
func TestRatioSamplerOwnedImageObjectPrivacy(t *testing.T) {
	for _, limit := range []int{128, -1} {
		for _, hide := range []bool{false, true} {
			t.Run(fmt.Sprintf("limit=%d/hide=%v", limit, hide), func(t *testing.T) {
				samplerEnvironment(t)
				t.Setenv("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", fmt.Sprint(limit))
				t.Setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "-1")
				processor := &ratioPrivacyProcessor{SpanRecorder: tracetest.NewSpanRecorder()}
				stop, err := InitForTest(context.Background(), WithSampleRate(.25), WithWireCapture(true), WithSpanProcessor(processor), WithTraceConfig(TraceConfig{HideInputImages: hide}))
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = stop(context.Background()) })

				parent := ratioParent(t, true, true, "fi=r:0.25")
				req, err := http.NewRequestWithContext(parent, http.MethodPost, "https://fixture.invalid/v1/chat/completions", strings.NewReader(`{"model":"fixture-model","messages":[{"role":"user","content":"hello"}]}`))
				if err != nil {
					t.Fatal(err)
				}
				req.Header.Set("Content-Type", "application/json")
				resp, err := Transport(ratioPrivacyTransport{}).RoundTrip(req)
				if err != nil {
					t.Fatal(err)
				}
				_, readErr := io.Copy(io.Discard, resp.Body)
				closeErr := resp.Body.Close()
				if readErr != nil || closeErr != nil {
					t.Fatalf("in-memory response: read=%v close=%v", readErr, closeErr)
				}
				if processor.provider == nil {
					t.Fatal("wire request did not expose the owned provider")
				}

				_, span := processor.provider.Tracer("ratio-privacy").Start(parent, "ratio-image", trace.WithAttributes(
					attribute.Int64("gen_ai.usage.input_tokens", 100),
					attribute.String("user.id", "ratio-user"),
					attribute.String("gen_ai.span.kind", "LLM"),
				))
				span.End()
				checkRatioAttribute(t, processor.raw, .25)
				if ratioPrivacyValues(processor.raw)["url"].AsString() != ratioPrivacyImageURL {
					t.Fatal("image hint fixture did not reach the raw span")
				}
				var exported sdktrace.ReadOnlySpan
				for _, ended := range processor.Ended() {
					if ended.Name() == "ratio-image" {
						exported = ended
					}
				}
				if exported == nil {
					t.Fatal("custom processor did not receive the sanitized span")
				}
				checkRatioAttribute(t, exported.Attributes(), .25)
				got := ratioPrivacyValues(exported.Attributes())
				if got["gen_ai.usage.input_tokens"] != attribute.Int64Value(100) || got["user.id"].AsString() != "ratio-user" || got["gen_ai.span.kind"].AsString() != "LLM" {
					t.Errorf("image-object recovery lost usage or identity: %v", exported.Attributes())
				}
				if !exported.SpanContext().IsSampled() || exported.SpanContext().TraceState().Get("fi") != "r:0.25" || exported.DroppedAttributes() != 0 {
					t.Error("fixture must remain sampled with ratio tracestate and no attribute pressure")
				}
				if hide {
					for _, key := range []string{"type", "url", "app.unrelated"} {
						if _, ok := got[key]; ok {
							t.Errorf("whole-object image recovery restored ordinary content %q", key)
						}
					}
				} else if got["url"].AsString() != ratioPrivacyImageURL || got["app.unrelated"].AsString() != "hidden-with-image-object" {
					t.Error("no-hide control did not preserve ordinary URL and content")
				}
			})
		}
	}
}

func ratioPrivacyTypedCases() []struct {
	name string
	kv   attribute.KeyValue
	keep bool
} {
	return []struct {
		name string
		kv   attribute.KeyValue
		keep bool
	}{
		{"fraction", attribute.Float64("fi.sampling.ratio", .25), true},
		{"smallest-positive", attribute.Float64("fi.sampling.ratio", math.SmallestNonzeroFloat64), true},
		{"below-one", attribute.Float64("fi.sampling.ratio", math.Nextafter(1, 0)), true},
		{"string", attribute.String("fi.sampling.ratio", "0.25"), false},
		{"integer", attribute.Int64("fi.sampling.ratio", 1), false},
		{"boolean", attribute.Bool("fi.sampling.ratio", true), false},
		{"string-slice", attribute.StringSlice("fi.sampling.ratio", []string{"0.25"}), false},
		{"integer-slice", attribute.Int64Slice("fi.sampling.ratio", []int64{1}), false},
		{"boolean-slice", attribute.BoolSlice("fi.sampling.ratio", []bool{true}), false},
		{"float-slice", attribute.Float64Slice("fi.sampling.ratio", []float64{.25}), false},
		{"generic-slice", attribute.KeyValue{Key: "fi.sampling.ratio", Value: attribute.SliceValue(attribute.Float64Value(.25))}, false},
		{"map", attribute.Map("fi.sampling.ratio", attribute.Float64("value", .25)), false},
		{"empty", attribute.KeyValue{Key: "fi.sampling.ratio"}, false},
		{"nan", attribute.Float64("fi.sampling.ratio", math.NaN()), false},
		{"positive-infinity", attribute.Float64("fi.sampling.ratio", math.Inf(1)), false},
		{"negative-infinity", attribute.Float64("fi.sampling.ratio", math.Inf(-1)), false},
		{"negative", attribute.Float64("fi.sampling.ratio", -.25), false},
		{"zero", attribute.Float64("fi.sampling.ratio", 0), false},
		{"negative-zero", attribute.Float64("fi.sampling.ratio", math.Copysign(0, -1)), false},
		{"one", attribute.Float64("fi.sampling.ratio", 1), false},
		{"above-one", attribute.Float64("fi.sampling.ratio", 1.25), false},
		{"suffix-neighbor", attribute.Float64("fi.sampling.ratio.extra", .25), false},
		{"prefix-neighbor", attribute.Float64("app.fi.sampling.ratio", .25), false},
		{"case-neighbor", attribute.Float64("FI.sampling.ratio", .25), false},
	}
}

// Protection is about the original OTel type and exact key, not coercibility.
// In particular NaN/Inf become strings during native conversion, but that must
// not make them eligible for recovery from a whole-object image replacement.
func TestRatioSamplerImageObjectTypedProtection(t *testing.T) {
	for _, tc := range ratioPrivacyTypedCases() {
		t.Run(tc.name, func(t *testing.T) {
			if got := protectedAttributePriority(tc.kv) >= 0; got != tc.keep {
				t.Errorf("protected classification = %v, want %v", got, tc.keep)
			}
			attrs := []attribute.KeyValue{
				attribute.String("type", "image_url"),
				attribute.String("url", ratioPrivacyImageURL),
				attribute.String("user.id", "ratio-user"),
				attribute.Int64("gen_ai.usage.input_tokens", 100),
				tc.kv,
			}
			s := &spanSanitizer{config: Config{TraceConfig: TraceConfig{HideInputImages: true}}, secrets: noSecrets, nodes: 16384, bytes: 32 * 1024 * 1024}
			got := ratioPrivacyValues(s.attrs(attrs, false, 0, 0))
			value, present := got[string(tc.kv.Key)]
			if present != tc.keep || tc.keep && (value.Type() != attribute.FLOAT64 || value.AsFloat64() != tc.kv.Value.AsFloat64()) {
				t.Errorf("image-object recovery: present=%v value=%v, want protected=%v", present, value, tc.keep)
			}
			if got["user.id"].AsString() != "ratio-user" || got["gen_ai.usage.input_tokens"] != attribute.Int64Value(100) {
				t.Error("image-object fallback lost its existing identity/usage protection")
			}
			if _, present := got["url"]; present {
				t.Error("image-object fallback restored the hidden URL")
			}
		})
	}
}

// Capture's bounded retry shares the protection classifier with export's
// image-object recovery. A fix must not accidentally grant arbitrary ratio-
// named values protection when the first joint walk exhausts its budget.
func TestRatioSamplerCaptureBudgetTypedProtection(t *testing.T) {
	for _, tc := range ratioPrivacyTypedCases() {
		t.Run(tc.name, func(t *testing.T) {
			out := captureContext([]attribute.KeyValue{
				tc.kv,
				attribute.String("user.id", "ratio-user"),
				attribute.Int64("gen_ai.usage.input_tokens", 100),
				attribute.Int64Slice("app.unrelated.samples", make([]int64, 17000)),
			}, nil, Config{}, "chat")
			got := ratioPrivacyValues(out)
			value, present := got[string(tc.kv.Key)]
			if present != tc.keep || tc.keep && (value.Type() != attribute.FLOAT64 || value.AsFloat64() != tc.kv.Value.AsFloat64()) {
				t.Errorf("capture budget recovery: present=%v value=%v, want protected=%v", present, value, tc.keep)
			}
			if !got["traceai.wire.privacy_degraded"].AsBool() || got["user.id"].AsString() != "ratio-user" || got["gen_ai.usage.input_tokens"] != attribute.Int64Value(100) {
				t.Error("budget fixture did not preserve degradation, identity and usage")
			}
			if _, present := got["app.unrelated.samples"]; present {
				t.Error("budget recovery restored unchecked unrelated content")
			}
		})
	}
}

func TestRatioSamplerImageObjectRecoveryUsesScrubbedValues(t *testing.T) {
	attrs := []attribute.KeyValue{
		attribute.Float64("fi.sampling.ratio", .25),
		attribute.String("type", "image_url"),
		attribute.String("url", ratioPrivacyImageURL),
		attribute.String("user.id", "ratio-user"),
		attribute.Int64("gen_ai.usage.input_tokens", 100),
		attribute.Map("project_name", attribute.String("data", r6aPCM)),
		attribute.String("project_name.mime_type", "audio/L16"),
		attribute.Map("project_type", attribute.String("type", "image_url"), attribute.String("url", ratioPrivacyImageURL)),
		attribute.String("session.id", "session-1"),
		attribute.String("app.unrelated", "hidden-with-image-object"),
	}
	s := &spanSanitizer{config: Config{TraceConfig: TraceConfig{HideInputImages: true}}, secrets: noSecrets, nodes: 16384, bytes: 32 * 1024 * 1024}
	out := s.attrs(attrs, false, 0, 0)
	checkRatioAttribute(t, out, .25)
	got := ratioPrivacyValues(out)
	if got["project_name"].Type() != attribute.MAP || strings.Contains(got["project_name"].Emit(), r6aPCM) {
		t.Error("protected project metadata was lost or restored without its catalog media scrub")
	}
	if got["project_type"].AsString() != RedactedValue {
		t.Error("protected image object bypassed per-key hiding during recovery")
	}
	if got["session.id"].AsString() != r6aJointSessionID(t) || got["user.id"].AsString() != "ratio-user" || got["gen_ai.usage.input_tokens"] != attribute.Int64Value(100) {
		t.Error("protected recovery bypassed catalog scrubbing or lost identity/usage")
	}
	for _, key := range []string{"type", "url", "app.unrelated", "project_name.mime_type"} {
		if _, present := got[key]; present {
			t.Errorf("whole-object recovery restored ordinary content %q", key)
		}
	}
}
