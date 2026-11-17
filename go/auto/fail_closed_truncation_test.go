package auto

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
)

// The two rc2f verifier reproductions: headerless PCM in a nested tool-argument
// JSON string (MIME before/after the payload), and a quoted ftyp literal in JSON
// or exception prose. All payloads are synthetic, and all exports are loopback.
type failClosedFixture struct{ key, value, payload string }

func failClosedFixtures() []failClosedFixture {
	pcm := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{0, 17, 34, 51}, 45))
	ftyp := base64.StdEncoding.EncodeToString(append([]byte{0, 0, 0, 24, 'f', 't', 'y', 'p', 'i', 's', 'o', 'm'}, bytes.Repeat([]byte{0, 17, 34, 51}, 18)...))
	var fixtures []failClosedFixture
	for i, clip := range []string{
		`{"clip":{"mimeType":"audio/L16","data":"` + pcm + `"}}`,
		`{"clip":{"data":"` + pcm + `","mimeType":"audio/L16"}}`,
	} {
		args, _ := json.Marshal(clip)
		value := `[{"role":"assistant","tool_calls":[{"function":{"name":"play","arguments":` + string(args) + `}}]},{"role":"user","content":"` + strings.Repeat("tail ", 60) + `"}]`
		// Keep the verifier's 647-character framing, with all named limits
		// cutting either the argument itself or the later ordinary message.
		value = value[:644] + `"}]`
		key := []string{"gen_ai.input.messages", "app.request"}[i]
		fixtures = append(fixtures, failClosedFixture{key, value, pcm})
	}
	return append(fixtures,
		failClosedFixture{"app.blob", `"` + ftyp + `"`, ftyp},
		failClosedFixture{"exception.message", `upload failed: "` + ftyp + `"`, ftyp})
}

func failClosedStrings(t *testing.T, v attribute.Value) []string {
	t.Helper()
	switch v.Type() {
	case attribute.STRING:
		return []string{v.AsString()}
	case attribute.STRINGSLICE:
		return v.AsStringSlice()
	case attribute.SLICE:
		var out []string
		for _, item := range v.AsSlice() {
			out = append(out, failClosedStrings(t, item)...)
		}
		return out
	case attribute.MAP:
		var out []string
		for _, item := range v.AsMap() {
			out = append(out, failClosedStrings(t, item.Value)...)
		}
		return out
	}
	t.Fatalf("string value/structure lost: %v", v.Type())
	return nil
}

var failClosedMarkers = regexp.MustCompile(`<(?:omitted: [^>]*|redacted)>`)

func checkFailClosedValue(t *testing.T, fixture failClosedFixture, limit int, values []string) {
	t.Helper()
	if len(values) == 0 {
		t.Fatal("attribute value dropped instead of scrubbed")
	}
	cut := fixture.value
	if limit >= 0 && limit < len(cut) {
		cut = cut[:limit]
	}
	start := strings.Index(fixture.value, fixture.payload)
	retained := min(len(fixture.payload), max(0, len(cut)-start))
	for _, got := range values {
		// Check every eight-character window, not only the payload prefix: a
		// registry marker or nested rewrite must not expose a later fragment.
		plain := failClosedMarkers.ReplaceAllString(got, " ")
		for start := 0; start+8 <= retained; start++ {
			if strings.Contains(plain, fixture.payload[start:start+8]) {
				t.Fatalf("%s at limit %d leaked a media run: %q", fixture.key, limit, got)
			}
		}
		if retained >= 8 && !strings.Contains(got, "<omitted:") {
			t.Fatalf("%s at limit %d lost its omission marker: %q", fixture.key, limit, got)
		}
	}
}

// Named reproductions use the real FI OTLP route on both ownership modes and
// every OTel value representation. Events/links are complete in this SDK and
// must continue to use precise catalog recognition, not the lexical fallback.
func TestFailClosedReproductionsOTLP(t *testing.T) {
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{-1, 10, 25, 34, 42, 53, 57, 377, 527, 637} {
			t.Run(fmt.Sprintf("owned=%v/limit=%d", owned, limit), func(t *testing.T) {
				tracer, host, received := rc2Destination(t, owned, map[string]int{"length": limit})
				for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
					var attrs []attribute.KeyValue
					for _, fixture := range failClosedFixtures() {
						attrs = append(attrs, rc2cAttr(rep, fixture.key, fixture.value))
					}
					attrs = append(attrs, attribute.String("app.ordinary", "ordinary_identifier_123456789"))
					_, span := tracer.Start(context.Background(), rep, trace.WithLinks(trace.Link{SpanContext: trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}}), Attributes: attrs}))
					span.SetAttributes(attrs...)
					span.AddEvent("event", trace.WithAttributes(attrs...))
					span.End()
				}
				request := flushedPrivacyExport(t, received)
				for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
					for _, fixture := range failClosedFixtures() {
						for _, surface := range []string{"span", "event", "link"} {
							v, ok := rc2bAttr(request, rep, surface, fixture.key)
							if !ok {
								t.Fatalf("%s/%s: lost %s", rep, surface, fixture.key)
							}
							var texts []string
							rc2AnyValues(v, func(v *commonpb.AnyValue) {
								if s, ok := v.Value.(*commonpb.AnyValue_StringValue); ok {
									texts = append(texts, s.StringValue)
								}
							})
							effective := limit
							if surface != "span" {
								effective = -1
							}
							checkFailClosedValue(t, fixture, effective, texts)
						}
					}
					for _, surface := range []string{"event", "link"} {
						v, _ := rc2bAttr(request, rep, surface, "app.ordinary")
						if v.GetStringValue() != "ordinary_identifier_123456789" {
							t.Fatalf("complete %s value changed: %q", surface, v.GetStringValue())
						}
					}
				}
				if host != nil {
					for _, span := range host.Ended() {
						for _, fixture := range failClosedFixtures() {
							want := fixture.value
							if limit >= 0 {
								want = want[:min(limit, len(want))]
							}
							for _, got := range failClosedStrings(t, spanAttr(span, fixture.key)) {
								if got != want {
									t.Fatal("agent mutated the host's original span")
								}
							}
						}
					}
				}
			})
		}
	}
}

// Sweep every SDK cut, not just the previously reproduced positions. A real
// registered agent destination (custom processor) exercises the same S1 export
// boundary without thousands of loopback collectors. No provider internals are
// mutated to change limits; each case initializes a fresh provider.
func TestFailClosedEverySDKCut(t *testing.T) {
	fixtures := failClosedFixtures()
	controls := failClosedOrdinaryAttributes()
	maxLength := 0
	for _, f := range fixtures {
		maxLength = max(maxLength, len(f.value))
	}
	for _, owned := range []bool{false, true} {
		for limit := 0; limit <= maxLength+1; limit++ {
			t.Run(fmt.Sprintf("owned=%v/cut=%d", owned, limit), func(t *testing.T) {
				if owned {
					ownedRegressionSetup(t)
				} else {
					exporterRegressionSetup(t)
				}
				t.Setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", strconv.Itoa(limit))
				sink, host := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
				opts := []Option{WithSpanProcessor(sink)}
				if !owned {
					p := sdktrace.NewTracerProvider()
					t.Cleanup(func() { _ = p.Shutdown(context.Background()) })
					opts = append(opts, WithTracerProvider(p))
				}
				if _, err := InitForTest(context.Background(), opts...); err != nil {
					t.Fatal(err)
				}
				p := agent.current.provider
				if agent.current.owned != owned {
					t.Fatal("wrong provider ownership")
				}
				p.RegisterSpanProcessor(host)
				for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
					var attrs []attribute.KeyValue
					for _, f := range fixtures {
						attrs = append(attrs, rc2cAttr(rep, f.key, f.value))
					}
					attrs = append(attrs, controls...)
					_, span := p.Tracer("app").Start(context.Background(), rep)
					// Include the agent projection before the SDK container too.
					span.SetAttributes(budgetAttributes(attrs, 32)...)
					span.End()
				}
				if len(sink.Ended()) != 4 || len(host.Ended()) != 4 {
					t.Fatal("span lost or duplicated")
				}
				for i, span := range sink.Ended() {
					for _, control := range controls {
						if !truncatedValue(control.Value, limit, 0) && !reflect.DeepEqual(spanAttr(span, string(control.Key)), control.Value) {
							t.Fatalf("complete sibling %s changed at limit %d", control.Key, limit)
						}
					}
					for _, f := range fixtures {
						value := spanAttr(span, f.key)
						original := spanAttr(host.Ended()[i], f.key)
						if value.Type() != original.Type() {
							t.Fatalf("%s: attribute representation lost", f.key)
						}
						checkFailClosedValue(t, f, limit, failClosedStrings(t, value))
						for _, raw := range failClosedStrings(t, original) {
							if raw != f.value[:min(limit, len(f.value))] {
								t.Fatalf("%s: host value changed or SDK did not cut", f.key)
							}
						}
					}
				}
			})
		}
	}
}

func failClosedOrdinaryAttributes() []attribute.KeyValue {
	return []attribute.KeyValue{
		attribute.String("app.word", "hellohellohellohellohello"),
		attribute.String("app.id", "identifier_0123456789"),
		attribute.String("app.number", "12345678901234567890"),
		attribute.String("app.note", "short ordinary prose stays unchanged"),
		attribute.String("app.json", `{"arguments":"{\"blob\":\"AbCdEf012\"}"}`),
		attribute.String("app.prefix", "data:audio/L16"),
		attribute.StringSlice("app.strings", []string{"AbCdEf012", "ordinary"}),
		attribute.Slice("app.slice", attribute.StringValue("AbCdEf012")),
		attribute.Map("app.map", attribute.String("item", "AbCdEf012")),
		attribute.Int64("app.integer", 123456789),
	}
}

func TestFailClosedUntruncatedOrdinaryAttributes(t *testing.T) {
	attrs := failClosedOrdinaryAttributes()
	for _, limit := range []int{-1, 1000} {
		s := &spanSanitizer{secrets: noSecrets, nodes: 16384, bytes: 32 * 1024 * 1024, risk: mediaRisk(attrs, 0, limit)}
		if got := s.attrs(attrs, false, 0, 0); !reflect.DeepEqual(got, attrs) {
			t.Fatalf("complete attributes changed (limit %d): %v", limit, got)
		}
	}
	// One cut string in a native tree does not taint complete siblings, even
	// when they are themselves base64-shaped, nor unrelated scalar attributes.
	values := map[string]any{"app.note": "failed: AbCdEf012", "app.other": "AbCdEf012", "app.tree": []any{"AbCdEf012", "failed: AbCdEf012"}}
	got := wire.PrivacyValueFailClosed(values, "", nil, nil, wire.MediaRisk{Truncated: func(s string) bool { return len(s) == 17 }}).(map[string]any)
	if got["app.other"] != values["app.other"] || got["app.tree"].([]any)[0] != "AbCdEf012" {
		t.Fatalf("cut state escaped to complete siblings: %v", got)
	}
}
