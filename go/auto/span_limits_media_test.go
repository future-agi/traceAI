package auto

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	"google.golang.org/protobuf/proto"
)

// rc2 defect class (2026-10-31, go-r6a final #1 and its variants): OTel SDK
// span limits (attribute count 128 or smaller host limits, per-event/per-link
// counts, value-length limits) or the agent's own projection/fallback could
// evict or truncate a MIME/type sibling (or the hint was below a metadata
// boundary) while the base64/binary payload stayed intact, so the export
// sanitizer exported raw audio. Agent-owned spans are recognized on the
// complete attribute set before projection; the export boundary fails closed
// whenever the SDK reports dropped attributes or a value may be truncated.

const (
	rc2Payload    = "AQIDBA==" // 4 bytes headerless PCM: no magic, MIME-only.
	rc2SampleBase = 7_350_000
)

type rc2Shape struct {
	name, parent, payloadField, hintField, hint string
	numeric                                     bool
}

func rc2Shapes() []rc2Shape {
	return []rc2Shape{
		{name: "mime-data", parent: "gen_ai.input.messages.%d.parts.0", payloadField: "data", hintField: "mime_type", hint: "audio/L16"},
		{name: "octet-stream-data", parent: "gen_ai.input.messages.%d.parts.0", payloadField: "data", hintField: "mime_type", hint: "application/octet-stream"},
		{name: "type-input-audio", parent: "gen_ai.input.messages.%d.parts.0", payloadField: "content", hintField: "type", hint: "input_audio"},
		{name: "below-transcript", parent: "input.transcript.parts.%d", payloadField: "data", hintField: "mime_type", hint: "audio/L16"},
		{name: "below-id", parent: "input.id.parts.%d", payloadField: "data", hintField: "mime_type", hint: "audio/L16"},
		{name: "integer-samples", parent: "gen_ai.usage.extension.%d", payloadField: "data", hintField: "mime_type", hint: "audio/L16", numeric: true},
	}
}

func (sh rc2Shape) attrs(index int, payload string) (data, hint attribute.KeyValue) {
	parent := fmt.Sprintf(sh.parent, index)
	data = attribute.String(parent+"."+sh.payloadField, payload)
	if sh.numeric {
		data = attribute.Int64Slice(parent+"."+sh.payloadField, []int64{rc2SampleBase + 1, rc2SampleBase + 2, rc2SampleBase + 3, rc2SampleBase + 4})
	}
	return data, attribute.String(parent+"."+sh.hintField, sh.hint)
}

// rc2CountLayout fills limit-1 slots with an ordinary long message list, puts
// the payload deep in it (the last free slot) and sets its hint and further
// messages afterwards, so a count limit evicts the hint but keeps the payload.
func rc2CountLayout(sh rc2Shape, limit int, payload string) (attrs []attribute.KeyValue, hintKey string) {
	for i := 0; i < limit-1; i++ {
		key, text := "gen_ai.input.messages.%d.content", "message %d"
		if i%2 == 1 {
			key, text = "gen_ai.input.messages.%d.role", "role %d"
		}
		attrs = append(attrs, attribute.String(fmt.Sprintf(key, i), fmt.Sprintf(text, i)))
	}
	data, hint := sh.attrs(limit+5, payload)
	attrs = append(attrs, data, hint)
	for i := 0; i < 20; i++ {
		attrs = append(attrs, attribute.String(fmt.Sprintf("gen_ai.output.messages.%d.content", i), "late message"))
	}
	return attrs, string(hint.Key)
}

func rc2AnyValues(v *commonpb.AnyValue, visit func(*commonpb.AnyValue)) {
	if v == nil {
		return
	}
	visit(v)
	if a := v.GetArrayValue(); a != nil {
		for _, item := range a.Values {
			rc2AnyValues(item, visit)
		}
	}
	if m := v.GetKvlistValue(); m != nil {
		for _, kv := range m.Values {
			rc2AnyValues(kv.Value, visit)
		}
	}
}

// rc2Leaks reports raw payload text or raw integer samples anywhere in an OTLP
// request: span, event and link attributes, names, status and resources.
func rc2Leaks(t *testing.T, request *collector.ExportTraceServiceRequest, payload string) []string {
	t.Helper()
	var leaks []string
	wire, _ := proto.Marshal(request)
	if bytes.Contains(wire, []byte(payload)) {
		leaks = append(leaks, "raw payload bytes")
	}
	if bytes.Contains(wire, []byte(strconv.Itoa(rc2SampleBase+1))) {
		leaks = append(leaks, "raw sample text")
	}
	check := func(where string, kvs []*commonpb.KeyValue) {
		for _, kv := range kvs {
			rc2AnyValues(kv.Value, func(v *commonpb.AnyValue) {
				if x, ok := v.Value.(*commonpb.AnyValue_IntValue); ok && x.IntValue > rc2SampleBase && x.IntValue < rc2SampleBase+100 {
					leaks = append(leaks, where+" raw integer sample "+kv.Key)
				}
			})
		}
	}
	for _, rs := range request.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			for _, span := range ss.Spans {
				check("span", span.Attributes)
				for _, event := range span.Events {
					check("event", event.Attributes)
				}
				for _, link := range span.Links {
					check("link", link.Attributes)
				}
			}
		}
	}
	return leaks
}

func rc2OTLPHas(request *collector.ExportTraceServiceRequest, text string) bool {
	wire, _ := proto.Marshal(request)
	return bytes.Contains(wire, []byte(text))
}

func rc2HasKey(request *collector.ExportTraceServiceRequest, key string) bool {
	for _, rs := range request.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			for _, span := range ss.Spans {
				for _, kv := range span.Attributes {
					if kv.Key == key {
						return true
					}
				}
			}
		}
	}
	return false
}

func rc2SpanDropped(request *collector.ExportTraceServiceRequest, name string) uint32 {
	for _, rs := range request.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			for _, span := range ss.Spans {
				if span.Name == name {
					return span.DroppedAttributesCount
				}
			}
		}
	}
	return 0
}

// rc2Destination initializes the agent (one-line init) on a host provider
// with the given limits, or on the agent-owned provider configured through
// the standard OTEL_* limit variables. It returns the application tracer, the
// host's own (unsanitized) recorder when there is one, and the collector.
func rc2Destination(t *testing.T, owned bool, limits map[string]int) (trace.Tracer, *tracetest.SpanRecorder, <-chan *collector.ExportTraceServiceRequest) {
	t.Helper()
	if owned {
		ownedRegressionSetup(t)
	} else {
		exporterRegressionSetup(t)
	}
	received := privacyCollector(t)
	env := map[string]string{"count": "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "length": "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "event": "OTEL_EVENT_ATTRIBUTE_COUNT_LIMIT", "link": "OTEL_LINK_ATTRIBUTE_COUNT_LIMIT"}
	for name := range env {
		if _, ok := limits[name]; !ok {
			t.Setenv(env[name], "") // Defaults unless a case sets the limit.
		}
	}
	if owned {
		for name, value := range limits {
			t.Setenv(env[name], strconv.Itoa(value))
		}
		if _, err := InitForTest(context.Background(), WithCredentials("RC2_OWNED_FI_KEY", "RC2_OWNED_FI_SECRET")); err != nil {
			t.Fatal(err)
		}
		if !agent.current.owned {
			t.Fatal("expected the agent-owned provider")
		}
		return agent.current.provider.Tracer("application"), nil, received
	}
	sl := sdktrace.NewSpanLimits()
	for name, value := range limits {
		switch name {
		case "count":
			sl.AttributeCountLimit = value
		case "length":
			sl.AttributeValueLengthLimit = value
		case "event":
			sl.AttributePerEventCountLimit = value
		case "link":
			sl.AttributePerLinkCountLimit = value
		}
	}
	host := tracetest.NewSpanRecorder()
	p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(sl), sdktrace.WithSpanProcessor(host))
	t.Cleanup(func() { _ = p.Shutdown(context.Background()) })
	if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("RC2_HOST_FI_KEY", "RC2_HOST_FI_SECRET")); err != nil {
		t.Fatal(err)
	}
	return p.Tracer("application"), host, received
}

func rc2HostKept(t *testing.T, host *tracetest.SpanRecorder, payload string) {
	t.Helper()
	if host == nil {
		return
	}
	// The application's own exporter keeps its original, unsanitized view.
	for _, span := range host.Ended() {
		for _, kv := range span.Attributes() {
			if strings.Contains(kv.Value.Emit(), payload) {
				return
			}
		}
	}
	t.Fatal("host view changed: the application's own span lost its payload")
}

// Count limits 32/128/140 and the SDK default, application spans with media
// deep in long message lists, on a host provider and the agent-owned one.
func TestCountLimitEvictedHintOTLP(t *testing.T) {
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{0, 32, 128, 140} { // 0: the SDK default (128).
			for _, sh := range rc2Shapes() {
				t.Run(fmt.Sprintf("owned=%v/limit=%d/%s", owned, limit, sh.name), func(t *testing.T) {
					limits := map[string]int{}
					effective := 128
					if limit > 0 {
						limits["count"], effective = limit, limit
					}
					tracer, host, received := rc2Destination(t, owned, limits)
					_, span := tracer.Start(context.Background(), "rc2-count")
					attrs, hintKey := rc2CountLayout(sh, effective, r6aPCM)
					span.SetAttributes(attrs...)
					span.End()
					request := flushedPrivacyExport(t, received)
					if leaks := rc2Leaks(t, request, r6aPCM); len(leaks) > 0 {
						t.Fatalf("media exported after its hint was evicted: %v", leaks)
					}
					if rc2HasKey(request, hintKey) || rc2SpanDropped(request, "rc2-count") == 0 {
						t.Fatal("scenario invalid: the SDK did not evict the hint")
					}
					// Text outside media-capable paths survives fail-closed mode.
					if !rc2OTLPHas(request, "role 3") {
						t.Fatal("ordinary metadata text lost in fail-closed mode")
					}
					if !sh.numeric {
						rc2HostKept(t, host, r6aPCM)
					}
				})
			}
		}
	}
}

// Value-length limits 8/16/24: a truncated MIME/type value is never a safe
// MIME, and a truncated payload prefix is still media.
func TestValueLengthTruncatedHintOTLP(t *testing.T) {
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{-1, 8, 16, 24} {
			for _, sh := range rc2Shapes() {
				t.Run(fmt.Sprintf("owned=%v/length=%d/%s", owned, limit, sh.name), func(t *testing.T) {
					limits := map[string]int{}
					if limit >= 0 {
						limits["length"] = limit
					}
					tracer, host, received := rc2Destination(t, owned, limits)
					_, span := tracer.Start(context.Background(), "rc2-length")
					data, hint := sh.attrs(3, rc2Payload)
					span.SetAttributes(hint, data, attribute.String("app.note", "two words"), attribute.String("app.word", "hello"))
					span.End()
					request := flushedPrivacyExport(t, received)
					if leaks := rc2Leaks(t, request, rc2Payload); len(leaks) > 0 {
						t.Fatalf("media exported beside a truncated hint: %v", leaks)
					}
					if !rc2OTLPHas(request, "two words") && (limit < 0 || limit >= 9) {
						t.Fatal("ordinary text lost")
					}
					if limit < 0 && !rc2OTLPHas(request, "hello") {
						t.Fatal("normal mode over-omitted a plain word")
					}
					if !sh.numeric {
						rc2HostKept(t, host, rc2Payload)
					}
				})
			}
		}
	}
}

// Truncated JSON-encoded attributes keep media ancestry in fail-closed mode:
// neither a payload prefix nor raw samples survive the partial scan.
func TestTruncatedJSONAttributeOTLP(t *testing.T) {
	pcm, _ := json.Marshal(map[string]any{"mime_type": "audio/L16", "data": r6aPCM})
	samples := fmt.Sprintf(`{"mime_type":"audio/L16","samples":[%d,%d,%d]}`, rc2SampleBase+1, rc2SampleBase+2, rc2SampleBase+3)
	nested := `{"part":{"data":["` + r6aPCM + `","` + r6aPCM + `"],"mime_type":"audio/L16"}}`
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{-1, 40, 48, 56} {
			t.Run(fmt.Sprintf("owned=%v/length=%d", owned, limit), func(t *testing.T) {
				limits := map[string]int{}
				if limit >= 0 {
					limits["length"] = limit
				}
				tracer, _, received := rc2Destination(t, owned, limits)
				_, span := tracer.Start(context.Background(), "rc2-json")
				span.SetAttributes(attribute.String("app.audio.part", string(pcm)), attribute.String("app.audio.samples", samples), attribute.String("app.nested", nested))
				span.End()
				request := flushedPrivacyExport(t, received)
				if leaks := rc2Leaks(t, request, r6aPCM[:12]); len(leaks) > 0 {
					t.Fatalf("truncated JSON exported media: %v", leaks)
				}
			})
		}
	}
}

// Per-event and per-link count limits (and value-length limits) on events and
// links: each attribute object fails closed on its own dropped count.
func TestEventAndLinkLimitsOTLP(t *testing.T) {
	for _, owned := range []bool{false, true} {
		for _, mode := range []string{"count", "length"} {
			for _, sh := range rc2Shapes() {
				t.Run(fmt.Sprintf("owned=%v/%s/%s", owned, mode, sh.name), func(t *testing.T) {
					limits := map[string]int{"event": 4, "link": 4}
					payload := r6aPCM
					if mode == "length" {
						limits = map[string]int{"length": 16}
						payload = rc2Payload
					}
					tracer, _, received := rc2Destination(t, owned, limits)
					data, hint := sh.attrs(2, payload)
					parent := strings.TrimSuffix(string(data.Key), "."+sh.payloadField)
					group := []attribute.KeyValue{data, attribute.String(parent+".f1", "filler text"), attribute.String(parent+".f2", "filler text"), attribute.String(parent+".f3", "filler text"), hint}
					_, other := tracer.Start(context.Background(), "rc2-link-target")
					other.End()
					_, span := tracer.Start(context.Background(), "rc2-events-links", trace.WithLinks(trace.Link{SpanContext: other.SpanContext(), Attributes: group}))
					span.AddEvent("rc2-event", trace.WithAttributes(group...))
					span.End()
					request := flushedPrivacyExport(t, received)
					if leaks := rc2Leaks(t, request, payload); len(leaks) > 0 {
						t.Fatalf("event/link media exported: %v", leaks)
					}
					if !rc2OTLPHas(request, "filler text") {
						t.Fatal("ordinary event/link text lost")
					}
					if mode == "count" && rc2OTLPHas(request, sh.hint) {
						t.Fatal("scenario invalid: per-event/per-link limits kept the hint")
					}
				})
			}
		}
	}
}

// A custom span processor replaces the FI exporter: its handoff is the same
// fail-closed boundary, using the provider's limits.
func TestCustomProcessorHandoffFailsClosed(t *testing.T) {
	for _, mode := range []string{"count", "length"} {
		t.Run(mode, func(t *testing.T) {
			exporterRegressionSetup(t)
			sl := sdktrace.NewSpanLimits()
			payload := r6aPCM
			if mode == "count" {
				sl.AttributeCountLimit = 32
			} else {
				sl.AttributeValueLengthLimit = 16
				payload = rc2Payload
			}
			p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(sl))
			defer p.Shutdown(context.Background())
			sink := tracetest.NewSpanRecorder()
			if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(sink)); err != nil {
				t.Fatal(err)
			}
			sh := rc2Shapes()[1]
			_, span := p.Tracer("application").Start(context.Background(), "rc2-processor")
			if mode == "count" {
				attrs, _ := rc2CountLayout(sh, 32, payload)
				span.SetAttributes(attrs...)
			} else {
				data, hint := sh.attrs(1, payload)
				span.SetAttributes(hint, data)
			}
			span.End()
			if len(sink.Ended()) != 1 {
				t.Fatal("custom processor lost the span")
			}
			for _, kv := range sink.Ended()[0].Attributes() {
				if strings.Contains(kv.Value.Emit(), payload) {
					t.Fatalf("raw media handed to the custom processor via %s", kv.Key)
				}
			}
		})
	}
}

// R6A go final #1: protected-context fallback with MIME hints carried as a
// native map, a JSON-object string or a generic slice, at count limits
// 32/128/140; an unrelated 17,000-element attribute exhausts the first walk.
func TestProtectedFallbackStructuredHintsOTLP(t *testing.T) {
	representations := map[string]attribute.KeyValue{
		"native-map":    attribute.Map("gen_ai.usage.extension", attribute.String("mime_type", "audio/L16")),
		"json-object":   attribute.String("gen_ai.usage.extension", `{"mime_type":"audio/L16"}`),
		"generic-slice": attribute.Slice("gen_ai.usage.extension.mime_type", attribute.StringValue("audio/L16")),
		"flat-control":  attribute.String("gen_ai.usage.extension.mime_type", "audio/L16"),
	}
	big := make([]int64, 17000)
	for i := range big {
		big[i] = int64(i)
	}
	for name, hint := range representations {
		for _, budget := range []bool{false, true} {
			for _, limit := range []int{32, 128, 140} {
				t.Run(fmt.Sprintf("%s/budget=%v/limit=%d", name, budget, limit), func(t *testing.T) {
					exporterRegressionSetup(t)
					received := privacyCollector(t)
					sl := sdktrace.NewSpanLimits()
					sl.AttributeCountLimit = limit
					host := tracetest.NewSpanRecorder()
					p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(sl), sdktrace.WithSpanProcessor(host))
					defer p.Shutdown(context.Background())
					if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("RC2_FALLBACK_FI_KEY", "RC2_FALLBACK_FI_SECRET")); err != nil {
						t.Fatal(err)
					}
					extra := []attribute.KeyValue{hint}
					for i := 0; i < 20; i++ {
						extra = append(extra, attribute.Int64(fmt.Sprintf("gen_ai.usage.extension.data.%03d", i), int64(r6aSampleBase+i)))
					}
					if budget {
						extra = append(extra, attribute.Int64Slice("app.unrelated.samples", big))
					}
					ctx := ContextWithUserID(ContextWithSessionID(context.Background(), "session-1"), "user-1")
					r6aLongChat(t, ContextWithAttributes(ctx, extra...), 80)
					request := flushedPrivacyExport(t, received)
					attrs := s6ProtoAttrs(request.ResourceSpans[0].ScopeSpans[0].Spans[0])
					for key, value := range attrs {
						if v, ok := value.(*commonpb.AnyValue); ok && strings.HasPrefix(key, "gen_ai.usage.extension.data") {
							if x, raw := v.Value.(*commonpb.AnyValue_IntValue); raw && x.IntValue >= r6aSampleBase {
								t.Fatalf("raw integer sample %s reached the OTLP export", key)
							}
						}
					}
					if len(host.Ended()) != 1 {
						t.Fatal("host provider lost the wire span")
					}
					if n := r6aRawSamples(host.Ended()[0].Attributes()); n != 0 {
						t.Fatalf("%d raw integer samples reached the host view", n)
					}
					for _, key := range []string{"user.id", "session.id", "gen_ai.span.kind", "gen_ai.usage.total_tokens"} {
						if attrs[key] == nil {
							t.Errorf("lost protected %s", key)
						}
					}
					if budget && attrs["traceai.wire.privacy_degraded"] == nil {
						t.Error("budget fallback lost its degradation marker")
					}
				})
			}
		}
	}
}

// Agent-created wire spans under host value-length limits: context media is
// recognized on the complete attribute set before the SDK truncates anything.
func TestWireSpanValueLengthOTLP(t *testing.T) {
	for _, limit := range []int{8, 16, 24} {
		t.Run(strconv.Itoa(limit), func(t *testing.T) {
			exporterRegressionSetup(t)
			received := privacyCollector(t)
			sl := sdktrace.NewSpanLimits()
			sl.AttributeValueLengthLimit = limit
			host := tracetest.NewSpanRecorder()
			p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(sl), sdktrace.WithSpanProcessor(host))
			defer p.Shutdown(context.Background())
			if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("RC2_WIRE_FI_KEY", "RC2_WIRE_FI_SECRET")); err != nil {
				t.Fatal(err)
			}
			ctx := ContextWithAttributes(context.Background(),
				attribute.String("z.record.mime_type", "application/octet-stream"), attribute.String("z.record.data", rc2Payload),
				attribute.String("z.part.type", "input_audio"), attribute.String("z.part.content", rc2Payload),
				attribute.String("z.voice.transcript.mime_type", "audio/L16"), attribute.String("z.voice.transcript.data", rc2Payload))
			r6aLongChat(t, ctx, 2)
			request := flushedPrivacyExport(t, received)
			if leaks := rc2Leaks(t, request, rc2Payload); len(leaks) > 0 {
				t.Fatalf("wire span media exported under a value-length limit: %v", leaks)
			}
			for _, span := range host.Ended() {
				for _, kv := range span.Attributes() {
					if strings.Contains(kv.Value.Emit(), rc2Payload) {
						t.Fatalf("raw media reached the host view via %s", kv.Key)
					}
				}
			}
		})
	}
}

// The agent's own A7 projection pins a hint in every representation (native
// map, JSON object string, generic slice) to the groups it governs, at every
// limit, deterministically.
func TestProjectionPinsStructuredHints(t *testing.T) {
	base := []attribute.KeyValue{attribute.String("gen_ai.span.kind", "LLM"), attribute.String("traceai.capture.layer", "wire"), attribute.Int64("gen_ai.usage.total_tokens", 3), attribute.String("output.value", "ok")}
	for i := 0; i < 6; i++ {
		base = append(base, attribute.String(fmt.Sprintf("gen_ai.input.messages.%d.message.content", i), "text"))
	}
	// Each hint sits in a lower-priority group than the payload it governs
	// (ungrouped/parameters vs output), so only pinning keeps them together.
	cases := map[string][]attribute.KeyValue{
		"native-map":    {attribute.Map("gen_ai.output", attribute.String("mime_type", "audio/L16")), attribute.String("gen_ai.output.data", "<omitted: media>")},
		"json-object":   {attribute.String("gen_ai.output", `{"mimeType":"audio/L16"}`), attribute.String("gen_ai.output.data", "<omitted: media>")},
		"generic-slice": {attribute.Slice("gen_ai.tool.mime_type", attribute.StringValue("audio/L16")), attribute.String("gen_ai.tool.call.result.data", "<omitted: media>")},
	}
	for name, extra := range cases {
		values := append(append([]attribute.KeyValue(nil), base...), extra...)
		hint, payload := string(extra[0].Key), string(extra[1].Key)
		separable := false
		for limit := 0; limit <= len(values)+2; limit++ {
			got := budgetAttributes(values, limit)
			reversed := append([]attribute.KeyValue(nil), values...)
			for i, j := 0, len(reversed)-1; i < j; i, j = i+1, j-1 {
				reversed[i], reversed[j] = reversed[j], reversed[i]
			}
			if !reflect.DeepEqual(got, budgetAttributes(reversed, limit)) || len(got) > limit {
				t.Fatalf("%s/%d: nondeterministic or over-budget projection", name, limit)
			}
			kept := map[string]bool{}
			for _, kv := range got {
				kept[string(kv.Key)] = true
			}
			if kept[payload] && !kept[hint] {
				t.Fatalf("%s/%d: payload admitted without its %s hint", name, limit, hint)
			}
			separable = separable || kept[payload] && len(got) < len(values)
		}
		if !separable {
			t.Fatalf("%s: no limit admitted the payload under pressure", name)
		}
	}
}

// Unit level: the fail-closed walk and its boundaries.
func TestFailClosedSanitizerUnit(t *testing.T) {
	walk := func(failClosed bool, attrs ...attribute.KeyValue) map[string]string {
		s := &spanSanitizer{secrets: noSecrets, nodes: 16384, bytes: 32 * 1024 * 1024}
		if failClosed {
			s.risk = &wire.MediaRisk{}
		}
		got := map[string]string{}
		for _, kv := range s.attrs(attrs, false, 0, 0) {
			got[string(kv.Key)] = kv.Value.Emit()
		}
		return got
	}
	got := walk(true,
		attribute.String("input.transcript.parts.0.data", rc2Payload),
		attribute.String("input.id.parts.0.data", rc2Payload),
		attribute.String("x.parts.0.mime_type", "application/octe"),
		attribute.String("x.parts.0.data", rc2Payload),
		attribute.String("x.input_audio.transcript", "Sure"),
		attribute.String("x.input_audio.id", "audio_123"),
		attribute.Int64("gen_ai.usage.input_tokens", 7),
		attribute.Int64Slice("gen_ai.usage.extension.data", []int64{1, 2}),
		attribute.String("user.id", "user-1"),
		attribute.String("gen_ai.input.messages.0.content", "two words"),
		attribute.String("app.word", "hello"),
	)
	// Every non-empty value under a media-capable path is omitted, plain text
	// included: without its MIME/type context it cannot be told from audio.
	for _, key := range []string{"input.transcript.parts.0.data", "input.id.parts.0.data", "x.parts.0.data", "gen_ai.usage.extension.data", "gen_ai.input.messages.0.content"} {
		if !strings.HasPrefix(got[key], "<omitted: ") {
			t.Errorf("fail-closed walk kept %s=%q", key, got[key])
		}
	}
	for key, want := range map[string]string{"x.input_audio.transcript": "Sure", "x.input_audio.id": "audio_123", "gen_ai.usage.input_tokens": "7", "user.id": "user-1", "app.word": "hello", "x.parts.0.mime_type": "application/octe"} {
		if got[key] != want {
			t.Errorf("fail-closed walk changed %s=%q, want %q", key, got[key], want)
		}
	}
	// Normal mode with intact context still omits; without context (and no
	// drop) the short field-named prefix is not media, as before.
	normal := walk(false, attribute.String("x.parts.0.mime_type", "application/octet-stream"), attribute.String("x.parts.0.data", rc2Payload), attribute.String("y.data", rc2Payload))
	if !strings.HasPrefix(normal["x.parts.0.data"], "<omitted: ") || normal["y.data"] != rc2Payload {
		t.Fatalf("normal-mode recognition changed: %v", normal)
	}
	// Detection: dropped counts and possible truncation, never otherwise.
	if incompleteAttributes([]attribute.KeyValue{attribute.String("a", "12345678")}, 0, -1) || incompleteAttributes([]attribute.KeyValue{attribute.String("a", "1234567")}, 0, 8) {
		t.Fatal("complete attributes reported incomplete")
	}
	if !incompleteAttributes(nil, 1, -1) || !incompleteAttributes([]attribute.KeyValue{attribute.String("a", "12345678")}, 0, 8) || !incompleteAttributes([]attribute.KeyValue{attribute.Map("m", attribute.StringSlice("s", []string{"", "123"}))}, 0, 3) || !incompleteAttributes([]attribute.KeyValue{attribute.String("a", "")}, 0, 0) {
		t.Fatal("dropped or truncated attributes not detected")
	}
}

// The value-length adapter reads the provider's immutable configuration.
func TestProviderValueLengthAdapter(t *testing.T) {
	for _, limit := range []int{-1, 0, 8, 16, 24} {
		sl := sdktrace.NewSpanLimits()
		sl.AttributeValueLengthLimit = limit
		p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(sl))
		if got := providerValueLengthLimit(p); got != limit {
			t.Fatalf("pinned SDK value-length adapter is stale: %d != %d", got, limit)
		}
		_ = p.Shutdown(context.Background())
	}
	if providerValueLengthLimit(nil) != 0 {
		t.Fatal("unknown provider must fail closed to a zero limit")
	}
}
