package auto

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// rc2c candidate-2 (2026-10-31, rc2 verifier finding 1): catalog-recognized
// ISO-BMFF base64 (ftyp at decoded offset 4) wrapped with CR/LF so that a
// value-length limit of 8/16/24 keeps only the four arbitrary box-size bytes:
// no signature byte survived, so the cut prefix ("AAAA\r\nEG") was exported
// raw at an arbitrary path, also below metadata boundaries. A cut prefix is
// now media whenever some continuation of it could satisfy a catalog rule.

// rc2cWrapped returns ftyp media whose first limit characters are "AAAA",
// CR/LF pairs and "EG": exactly 4 decoded bytes, all before the signature.
func rc2cWrapped(limit int) string {
	e := base64.StdEncoding.EncodeToString(append([]byte{0, 0, 0, 0x10, 'f', 't', 'y', 'p', 'i', 's', 'o', 'm'}, bytes.Repeat([]byte{0x30}, 24)...))
	pairs := 1
	if limit > 6 {
		pairs = (limit - 6) / 2
	}
	return e[:4] + strings.Repeat("\r\n", pairs) + e[4:]
}

// rc2cAttr is one representation of value at path: scalar string, string
// array, generic OTel slice, or native OTel map (value under a non-media key).
func rc2cAttr(rep, path, value string) attribute.KeyValue {
	switch rep {
	case "string-array":
		return attribute.StringSlice(path, []string{value, value})
	case "generic-slice":
		return attribute.Slice(path, attribute.StringValue(value))
	case "native-map":
		return attribute.Map(path, attribute.String("item", value))
	}
	return attribute.String(path, value)
}

// rc2cHostHas reports the application's own (unsanitized) view holding text
// in any string of any representation.
func rc2cHostHas(v attribute.Value, text string) bool {
	switch v.Type() {
	case attribute.STRING:
		return strings.Contains(v.AsString(), text)
	case attribute.STRINGSLICE:
		for _, s := range v.AsStringSlice() {
			if strings.Contains(s, text) {
				return true
			}
		}
	case attribute.SLICE:
		for _, item := range v.AsSlice() {
			if rc2cHostHas(item, text) {
				return true
			}
		}
	case attribute.MAP:
		for _, field := range v.AsMap() {
			if rc2cHostHas(field.Value, text) {
				return true
			}
		}
	}
	return false
}

// One-line init on a host provider and on the agent-owned provider: value
// limits 8/16/24 (and an unlimited control) at app.arbitrary and below the
// id/transcript metadata boundaries, in every attribute representation, on
// span attributes, events and links. Raw media never reaches FI OTLP; the
// application's own exporter keeps its original view.
func TestRC2cWrappedMagicPrefixOTLP(t *testing.T) {
	paths := []string{"app.arbitrary", "input.id.parts.9.arbitrary", "app.transcript.parts.83.arbitrary"}
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{-1, 8, 16, 24} {
			for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
				t.Run(fmt.Sprintf("owned=%v/length=%d/%s", owned, limit, rep), func(t *testing.T) {
					media := rc2cWrapped(limit)
					// The SDK keeps exactly limit characters: "AAAA", CR/LF, "EG".
					if limit > 0 && media[:limit] != "AAAA"+strings.Repeat("\r\n", (limit-6)/2)+"EG" {
						t.Fatalf("scenario invalid: %q", media[:limit])
					}
					limits := map[string]int{}
					if limit >= 0 {
						limits["length"] = limit
					}
					tracer, host, received := rc2Destination(t, owned, limits)
					attrs := []attribute.KeyValue{attribute.String("app.note", "two words")}
					for _, path := range paths {
						attrs = append(attrs, rc2cAttr(rep, path, media))
					}
					_, other := tracer.Start(context.Background(), "rc2c-link-target")
					other.End()
					_, span := tracer.Start(context.Background(), "rc2c-wrapped", trace.WithLinks(trace.Link{SpanContext: other.SpanContext(), Attributes: attrs}))
					span.SetAttributes(attrs...)
					span.AddEvent("rc2c-event", trace.WithAttributes(attrs...))
					span.End()
					request := flushedPrivacyExport(t, received)
					if key := rc2bRawString(request, media); key != "" {
						t.Fatalf("raw ftyp media (or its cut prefix) exported via %s", key)
					}
					if rc2OTLPHas(request, "AAAA\r\n") {
						t.Fatal("raw wrapped media bytes reached FI OTLP")
					}
					for _, path := range paths {
						if v, ok := rc2bAttr(request, "rc2c-wrapped", "span", path); !ok || v == nil {
							t.Fatalf("%s lost instead of omitted", path)
						}
					}
					if !rc2OTLPHas(request, "two word") {
						t.Fatal("ordinary text lost")
					}
					if host != nil {
						kept := false
						for _, s := range host.Ended() {
							for _, kv := range s.Attributes() {
								kept = kept || rc2cHostHas(kv.Value, "AAAA\r\n")
							}
						}
						if !kept {
							t.Fatal("host view changed: the application's own span lost its value")
						}
					}
				})
			}
		}
	}
}
