package auto

import (
	"context"
	"encoding/base64"
	"fmt"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
)

// The fail-closed lexical scan must not split a registered secret before S5
// sees it. Exercise both registry sources at the real FI destination, not just
// a mock masker. These synthetic punctuated secrets previously leaked their
// short segments even though the entire registered value survived the SDK cut.
func TestFailClosedRegisteredSecretsOTLP(t *testing.T) {
	const envSecret = "ab.cd.ef.gh.ijklmnopqrst"
	const explicitSecret = "Kx9!Summer2024secretvalue"
	for _, owned := range []bool{false, true} {
		for _, limit := range []int{-1, 60} {
			t.Run(fmt.Sprintf("owned=%v/limit=%d", owned, limit), func(t *testing.T) {
				t.Setenv("APP_DB_PASSWORD", envSecret)
				tracer, host, received := rc2Destination(t, owned, map[string]int{"length": limit})
				RegisterSecret(explicitSecret)
				for source, secret := range []string{envSecret, explicitSecret} {
					for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
						attrs := []attribute.KeyValue{
							rc2cAttr(rep, "app.error", "connect failed with password "+secret+strings.Repeat(" retry ", 20)),
							rc2cAttr(rep, "app.quoted", `failed: "`+secret+`"`+strings.Repeat(" retry ", 20)),
							rc2cAttr(rep, "app.json", `{"args":"{\"note\":\"`+secret+`\",\"tail\":\"`+strings.Repeat(" retry ", 20)+`\"}"}`),
							rc2cAttr(rep, "app.short", secret),
						}
						name := fmt.Sprintf("%d/%s", source, rep)
						_, span := tracer.Start(context.Background(), name, trace.WithLinks(trace.Link{
							SpanContext: trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}}), Attributes: attrs,
						}))
						span.SetAttributes(attrs...)
						span.AddEvent("event", trace.WithAttributes(attrs...))
						span.End()
					}
				}
				request := flushedPrivacyExport(t, received)
				for source, secret := range []string{envSecret, explicitSecret} {
					for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
						for _, key := range []string{"app.error", "app.quoted", "app.json", "app.short"} {
							for _, surface := range []string{"span", "event", "link"} {
								v, ok := rc2bAttr(request, fmt.Sprintf("%d/%s", source, rep), surface, key)
								if !ok {
									t.Fatalf("%s/%s: lost %s", rep, surface, key)
								}
								count := 0
								rc2AnyValues(v, func(v *commonpb.AnyValue) {
									text, ok := v.Value.(*commonpb.AnyValue_StringValue)
									if !ok {
										return
									}
									count++
									got := text.StringValue
									if !strings.Contains(got, wire.CredentialRedacted) {
										t.Errorf("%s/%s/%s: whole secret was not masked: %q", rep, surface, key, got)
									}
									for _, fragment := range []string{secret, "ab.cd.ef.gh.", "ijklmnopqrst", "Kx9!", "Summer2024secretvalue"} {
										if strings.Contains(got, fragment) {
											t.Errorf("%s/%s/%s: registered-secret fragment leaked: %q", rep, surface, key, got)
										}
									}
									if key == "app.short" && got != wire.CredentialRedacted {
										t.Errorf("complete sibling changed beyond redaction: %q", got)
									}
								})
								want := 1
								if rep == "string-array" {
									want = 2
								}
								if count != want {
									t.Fatalf("%s/%s/%s: lost string values: %d", rep, surface, key, count)
								}
							}
						}
					}
					rc2HostKept(t, host, secret)
				}
			})
		}
	}
}

// A quote or prose prefix consumes part of the SDK budget. The decoded final
// token inherits the enclosing cut even when its own length is below the limit.
func TestFailClosedQuotedShortMagicOTLP(t *testing.T) {
	ftyp := failClosedFixtures()[2].payload
	png := base64.StdEncoding.EncodeToString(append([]byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}, []byte("synthetic payload")...))
	for _, owned := range []bool{false, true} {
		for _, frame := range []struct{ key, prefix string }{{"app.blob", `"`}, {"exception.message", `upload failed: "`}} {
			for n := 1; n <= 7; n++ {
				t.Run(fmt.Sprintf("owned=%v/%s/payload=%d", owned, frame.key, n), func(t *testing.T) {
					tracer, host, received := rc2Destination(t, owned, map[string]int{"length": len(frame.prefix) + n})
					for _, payload := range []string{ftyp, png} {
						for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
							_, span := tracer.Start(context.Background(), payload[:8]+"/"+rep)
							span.SetAttributes(rc2cAttr(rep, frame.key, frame.prefix+payload+`"`))
							span.End()
						}
					}
					request := flushedPrivacyExport(t, received)
					for _, payload := range []string{ftyp, png} {
						want := frame.prefix + fmt.Sprintf("<omitted: %d base64 chars>\"", n)
						for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
							v, ok := rc2bAttr(request, payload[:8]+"/"+rep, "span", frame.key)
							if !ok {
								t.Fatalf("%s: attribute lost", rep)
							}
							texts := 0
							rc2AnyValues(v, func(v *commonpb.AnyValue) {
								if text, ok := v.Value.(*commonpb.AnyValue_StringValue); ok {
									texts++
									if text.StringValue != want {
										t.Errorf("%s: short quoted magic survived: got %q, want %q", rep, text.StringValue, want)
									}
								}
							})
							if texts == 0 {
								t.Fatal("string representation lost")
							}
						}
						rc2HostKept(t, host, frame.prefix+payload[:n])
					}
				})
			}
		}
	}
}
