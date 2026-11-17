package auto

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
)

// rc2 candidate-2 (2026-10-31, rc2 verifier finding 1): the fail-closed export
// predicate required length >= 4, the base64 alphabet AND a media-capable
// path, so short encoded audio ("AQ", "AQI"), raw binary bytes, whitespace-
// wrapped base64 and cut magic prefixes escaped once an SDK limit evicted or
// cut their MIME/type sibling; and context hints governing wire payloads were
// not applied before the SDK truncated them. Now every non-empty value under a
// media-capable path in an at-risk scope is omitted, a cut magic prefix is
// media anywhere, and wire payloads are walked with context hints at capture.

var rc2bPayloads = map[string]string{
	"two-chars":   "AQ",
	"three-chars": "AQI",
	"binary":      "\x01\x02\x03\x04\x05",
	"whitespace":  "AQID BAUG\tBwgJ",
	"padded":      "AQIDBA==", // Control: was already protected.
}

// rc2bAttr finds one attribute (span, event or link surface) of a named span.
func rc2bAttr(request *collector.ExportTraceServiceRequest, span, surface, key string) (*commonpb.AnyValue, bool) {
	for _, rs := range request.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			for _, s := range ss.Spans {
				if s.Name != span {
					continue
				}
				kvs := s.Attributes
				switch surface {
				case "event":
					kvs = nil
					for _, e := range s.Events {
						kvs = append(kvs, e.Attributes...)
					}
				case "link":
					kvs = nil
					for _, l := range s.Links {
						kvs = append(kvs, l.Attributes...)
					}
				}
				for _, kv := range kvs {
					if kv.Key == key {
						return kv.Value, true
					}
				}
			}
		}
	}
	return nil, false
}

// rc2bRawString reports any exported string value (any surface) equal to the
// raw payload, or a raw prefix of it of 3+ characters.
func rc2bRawString(request *collector.ExportTraceServiceRequest, payload string) string {
	found := ""
	check := func(kvs []*commonpb.KeyValue) {
		for _, kv := range kvs {
			rc2AnyValues(kv.Value, func(v *commonpb.AnyValue) {
				if s, ok := v.Value.(*commonpb.AnyValue_StringValue); ok && (s.StringValue == payload || len(s.StringValue) >= 3 && strings.HasPrefix(payload, s.StringValue)) {
					found = kv.Key
				}
			})
		}
	}
	for _, rs := range request.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			for _, s := range ss.Spans {
				check(s.Attributes)
				for _, e := range s.Events {
					check(e.Attributes)
				}
				for _, l := range s.Links {
					check(l.Attributes)
				}
			}
		}
	}
	return found
}

func rc2bOmitted(v *commonpb.AnyValue) bool {
	return v != nil && strings.HasPrefix(v.GetStringValue(), "<omitted: ")
}

// Default and count limits 32/128/140: the payload sits deep in a long message
// list below a metadata boundary (app.transcript), its MIME is evicted; on
// span attributes, events and links, host and agent-owned providers.
func TestCountLimitShortBinaryPayloadsOTLP(t *testing.T) {
	const dataKey, hintKey = "app.transcript.parts.83.data", "app.transcript.parts.83.mime_type"
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{0, 32, 128, 140} { // 0: SDK defaults (128).
			for name, payload := range rc2bPayloads {
				t.Run(fmt.Sprintf("owned=%v/limit=%d/%s", owned, limit, name), func(t *testing.T) {
					limits, effective := map[string]int{}, 128
					if limit > 0 {
						limits = map[string]int{"count": limit, "event": limit, "link": limit}
						effective = limit
					}
					tracer, host, received := rc2Destination(t, owned, limits)
					var attrs []attribute.KeyValue
					for i := 0; i < effective-1; i++ {
						key, text := "gen_ai.input.messages.%d.content", "message %d"
						if i%2 == 1 {
							key, text = "gen_ai.input.messages.%d.role", "role %d"
						}
						attrs = append(attrs, attribute.String(fmt.Sprintf(key, i), fmt.Sprintf(text, i)))
					}
					attrs = append(attrs, attribute.String(dataKey, payload), attribute.String(hintKey, "audio/L16"))
					_, target := tracer.Start(context.Background(), "rc2b-target")
					target.End()
					_, span := tracer.Start(context.Background(), "rc2b-count", trace.WithLinks(trace.Link{SpanContext: target.SpanContext(), Attributes: attrs}))
					span.SetAttributes(attrs...)
					span.AddEvent("rc2b-event", trace.WithAttributes(attrs...))
					span.End()
					request := flushedPrivacyExport(t, received)
					for _, surface := range []string{"span", "event", "link"} {
						data, kept := rc2bAttr(request, "rc2b-count", surface, dataKey)
						if _, hinted := rc2bAttr(request, "rc2b-count", surface, hintKey); !kept || hinted {
							t.Fatalf("%s: scenario invalid: payload kept=%v hint kept=%v", surface, kept, hinted)
						}
						if !rc2bOmitted(data) {
							t.Fatalf("%s: media exported after its MIME was evicted: %q", surface, data.GetStringValue())
						}
						if role, ok := rc2bAttr(request, "rc2b-count", surface, "gen_ai.input.messages.3.role"); !ok || role.GetStringValue() != "role 3" {
							t.Fatalf("%s: non-media metadata changed", surface)
						}
					}
					if key := rc2bRawString(request, payload); key != "" {
						t.Fatalf("raw payload exported via %s", key)
					}
					rc2HostKept(t, host, payload)
				})
			}
		}
	}
}

// Value-length limits 8/16/24 below a metadata boundary (input.id): a MIME the
// catalog accepts (leading whitespace) is cut, so it is never a safe MIME.
func TestValueLengthCutMIMEBelowIDOTLP(t *testing.T) {
	const dataKey, hintKey = "input.id.parts.9.data", "input.id.parts.9.mime_type"
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{-1, 8, 16, 24} {
			for _, name := range []string{"three-chars", "binary", "two-chars", "whitespace"} {
				payload := rc2bPayloads[name]
				t.Run(fmt.Sprintf("owned=%v/length=%d/%s", owned, limit, name), func(t *testing.T) {
					limits := map[string]int{}
					if limit >= 0 {
						limits["length"] = limit
					}
					tracer, host, received := rc2Destination(t, owned, limits)
					attrs := []attribute.KeyValue{attribute.String(hintKey, " application/octet-stream"), attribute.String(dataKey, payload), attribute.String("app.note", "ok")}
					_, target := tracer.Start(context.Background(), "rc2b-target")
					target.End()
					_, span := tracer.Start(context.Background(), "rc2b-length", trace.WithLinks(trace.Link{SpanContext: target.SpanContext(), Attributes: attrs}))
					span.SetAttributes(attrs...)
					span.AddEvent("rc2b-event", trace.WithAttributes(attrs...))
					span.End()
					request := flushedPrivacyExport(t, received)
					for _, surface := range []string{"span", "event", "link"} {
						if data, _ := rc2bAttr(request, "rc2b-length", surface, dataKey); !rc2bOmitted(data) {
							t.Fatalf("%s: media exported beside a cut MIME: %q", surface, data.GetStringValue())
						}
						if note, _ := rc2bAttr(request, "rc2b-length", surface, "app.note"); note.GetStringValue() != "ok" {
							t.Fatalf("%s: unrelated text changed", surface)
						}
					}
					if key := rc2bRawString(request, payload); key != "" {
						t.Fatalf("raw payload exported via %s", key)
					}
					rc2HostKept(t, host, payload[:2])
				})
			}
		}
	}
}

func rc2bMagic() map[string]string {
	pad := bytes.Repeat([]byte{0x30}, 24)
	return map[string]string{
		"png": base64.StdEncoding.EncodeToString(append([]byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}, pad...)),
		"wav": base64.StdEncoding.EncodeToString(append([]byte("RIFF0000WAVEfmt "), pad...)),
		"mp4": base64.StdEncoding.EncodeToString(append([]byte{0, 0, 0, 0x10, 'f', 't', 'y', 'p'}, pad...)),
	}
}

// A value-length limit cuts recognized PNG/WAV/MP4 base64 under an arbitrary
// key to a prefix with no full signature: still media.
func TestTruncatedMagicPrefixOTLP(t *testing.T) {
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{-1, 8, 16, 24} {
			for name, media := range rc2bMagic() {
				t.Run(fmt.Sprintf("owned=%v/length=%d/%s", owned, limit, name), func(t *testing.T) {
					limits := map[string]int{}
					if limit >= 0 {
						limits["length"] = limit
					}
					tracer, host, received := rc2Destination(t, owned, limits)
					_, span := tracer.Start(context.Background(), "rc2b-magic")
					span.SetAttributes(attribute.String("app.arbitrary", media), attribute.String("app.word", "hellohellohellohellohello"))
					span.End()
					request := flushedPrivacyExport(t, received)
					if v, _ := rc2bAttr(request, "rc2b-magic", "span", "app.arbitrary"); !rc2bOmitted(v) {
						t.Fatalf("cut %s prefix exported raw: %q", name, v.GetStringValue())
					}
					// Cut 8+ base64-alphabet runs are now suspect, even all-letter
					// text; unlimited values retain precise catalog recognition.
					if v, _ := rc2bAttr(request, "rc2b-magic", "span", "app.word"); rc2bOmitted(v) != (limit >= 0) {
						t.Fatal("base64-shaped text did not respect cut vs complete mode")
					}
					rc2HostKept(t, host, media[:8])
				})
			}
		}
	}
}

// rc2bChat sends a real loopback chat whose assistant content is reply.
func rc2bChat(t *testing.T, ctx context.Context, reply string) {
	t.Helper()
	body := `{"model":"m","messages":[{"role":"user","content":"hi"}]}`
	response, _ := json.Marshal(map[string]any{"id": "c1", "model": "m", "choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": reply}, "finish_reason": "stop"}}, "usage": map[string]any{"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(response)
	}))
	defer provider.Close()
	req, _ := http.NewRequestWithContext(ctx, "POST", provider.URL+"/v1/chat/completions", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	data, readErr := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if readErr != nil || !bytes.Equal(data, response) {
		t.Fatal("agent changed the application response")
	}
}

// Agent-created wire span: a context hint (output.type=input_audio) governs
// the wire's own output.value; it is applied at capture, before the SDK cuts
// the hint to "input_au".
func TestWireContextHintGovernsWirePayloadOTLP(t *testing.T) {
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{-1, 8, 16, 24} {
			t.Run(fmt.Sprintf("owned=%v/length=%d", owned, limit), func(t *testing.T) {
				limits := map[string]int{}
				if limit >= 0 {
					limits["length"] = limit
				}
				_, host, received := rc2Destination(t, owned, limits)
				rc2bChat(t, ContextWithAttributes(context.Background(), attribute.String("output.type", "input_audio")), "AQI")
				request := flushedPrivacyExport(t, received)
				var v *commonpb.AnyValue
				ok := false
				for _, rs := range request.ResourceSpans {
					for _, ss := range rs.ScopeSpans {
						for _, s := range ss.Spans {
							if found, has := rc2bAttr(request, s.Name, "span", "output.value"); has {
								v, ok = found, true
							}
						}
					}
				}
				if !ok || v.GetStringValue() == "AQI" || strings.HasPrefix(v.GetStringValue(), "AQ") || !strings.HasPrefix(v.GetStringValue(), "<omitted") {
					t.Fatalf("wire output.value exported raw under a context audio hint: %q", v.GetStringValue())
				}
				if host != nil {
					for _, span := range host.Ended() {
						for _, kv := range span.Attributes() {
							if kv.Key == "output.value" && kv.Value.AsString() == "AQI" {
								t.Fatal("raw wire payload reached the host view")
							}
						}
					}
				}
			})
		}
	}
}

// Without a context hint the wire span's capture is unchanged.
func TestCaptureWireUnchangedWithoutContextHint(t *testing.T) {
	values := []attribute.KeyValue{attribute.String("output.value", "AQI"), attribute.String("output.mime_type", "text/plain")}
	got := captureWire(values, []attribute.KeyValue{attribute.String("user.id", "u")}, Config{}, "chat")
	if len(got) != 2 || got[0] != values[0] || got[1] != values[1] {
		t.Fatalf("capture changed without a context hint: %v", got)
	}
	for _, hint := range []attribute.KeyValue{
		attribute.String("output.type", "input_audio"),
		attribute.Map("output", attribute.String("mime_type", "audio/L16")),
		attribute.String("output", `{"mimeType":"audio/L16"}`),
	} {
		got := captureWire(values, []attribute.KeyValue{hint}, Config{}, "chat")
		if len(got) != 2 || !strings.HasPrefix(got[0].Value.AsString(), "<omitted: ") || got[1].Value.AsString() != "text/plain" {
			t.Fatalf("context hint %s not applied to the wire payload: %v", hint.Key, got)
		}
	}
}

// Risk scoping: a cut value only puts its hint parent's subtree at risk;
// dropped attributes put everything at risk.
func TestMediaRiskScopes(t *testing.T) {
	attrs := []attribute.KeyValue{attribute.String("input.id.parts.9.mime_type", " applica"), attribute.String("input.id.parts.9.data", "AQI"), attribute.String("other.value", "plain"), attribute.String("app.note", "ok")}
	walk := func(risk *wire.MediaRisk) map[string]string {
		s := &spanSanitizer{secrets: noSecrets, nodes: 16384, bytes: 32 * 1024 * 1024, risk: risk}
		got := map[string]string{}
		for _, kv := range s.attrs(attrs, false, 0, 0) {
			got[string(kv.Key)] = kv.Value.Emit()
		}
		return got
	}
	scoped := walk(mediaRisk(attrs, 0, 8))
	if !strings.HasPrefix(scoped["input.id.parts.9.data"], "<omitted: ") || scoped["other.value"] != "plain" || scoped["app.note"] != "ok" {
		t.Fatalf("truncation scope wrong: %v", scoped)
	}
	all := walk(mediaRisk(attrs, 1, -1))
	if !strings.HasPrefix(all["other.value"], "<omitted: ") || all["app.note"] != "ok" {
		t.Fatalf("dropped attributes must put every media-capable path at risk: %v", all)
	}
	if mediaRisk(attrs, 0, 64) != nil || mediaRisk(attrs, 0, -1) != nil {
		t.Fatal("complete attributes reported at risk")
	}
}
