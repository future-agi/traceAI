package auto

import (
	"context"
	"fmt"
	"net/url"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
)

// rc2f: SDK truncation can remove a URL query's MIME sibling or the magic
// bytes of its final value. The containing attribute need not be a media path.
func TestQueryValueLimitsOTLP(t *testing.T) {
	fixture := "https://example.invalid/?content=AQIDBA%3D%3D&mimeType=audio%2Fwav&model=keep#anchor"
	pcm := strings.Repeat("AQIDBAUG", 30)
	magic := "AAAAIGZ0eXBpc29tMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
	jsonPrefix := `{"link":"https:\/\/example.invalid\/?v=\/9j\/`
	jsonMedia := jsonPrefix + strings.Repeat(`\/`, 24) + `"}`
	cases := []struct {
		name, value, raw string
		limits           []int
	}{
		{"fixture", fixture, "AQIDBA", []int{-1, 45, 50, 57}},
		{"pcm", "https://example.invalid/?data=" + pcm + "&mimeType=audio/pcm", "AQIDBAUG", []int{-1, 80, 120, 200, 260}},
		{"magic", "https://example.invalid/?v=" + magic, "AAAA", []int{-1, 31, 33, 35, 37}},
		{"wrapped", "https://example.invalid/?v=" + url.QueryEscape(rc2cWrapped(8)), "AAAA", []int{-1, 31, 32, 33, 34, 35, 36, 38}},
		{"data-uri", "https://example.invalid/?v=" + url.QueryEscape("data:audio/wav;base64,AQIDBA=="), "AQIDBA", []int{-1, 63}},
		{"json-escape", jsonMedia, `\/9j\/`, []int{-1, len(jsonPrefix) + 1, len(jsonPrefix) + 3}},
	}
	for _, owned := range []bool{false, true} {
		for _, tc := range cases {
			for _, limit := range tc.limits {
				for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
					t.Run(fmt.Sprintf("owned=%v/%s/length=%d/%s", owned, tc.name, limit, rep), func(t *testing.T) {
						tracer, host, received := rc2Destination(t, owned, map[string]int{"length": limit})
						attrs := []attribute.KeyValue{
							rc2cAttr(rep, "app.link", tc.value),
							rc2cAttr(rep, "exception.message", "failed "+tc.value),
							attribute.String("app.note", "two words"),
						}
						_, target := tracer.Start(context.Background(), "query-target")
						target.End()
						_, span := tracer.Start(context.Background(), "query-limits", trace.WithLinks(trace.Link{SpanContext: target.SpanContext(), Attributes: attrs}))
						span.SetAttributes(attrs...)
						span.AddEvent("query-event", trace.WithAttributes(attrs...))
						span.End()
						request := flushedPrivacyExport(t, received)
						for _, surface := range []string{"span", "event", "link"} {
							for _, key := range []string{"app.link", "exception.message"} {
								value, ok := rc2bAttr(request, "query-limits", surface, key)
								if !ok {
									t.Fatalf("lost %s %s", surface, key)
								}
								rc2AnyValues(value, func(v *commonpb.AnyValue) {
									if strings.Contains(v.GetStringValue(), tc.raw) {
										t.Errorf("raw query media at %s %s: %q", surface, key, v.GetStringValue())
									}
								})
							}
						}
						if !rc2OTLPHas(request, "two words") {
							t.Fatal("ordinary text lost")
						}
						if host != nil {
							want := tc.value
							if limit >= 0 && limit < len(want) {
								want = want[:limit]
							}
							kept := false
							for _, s := range host.Ended() {
								for _, kv := range s.Attributes() {
									kept = kept || rc2cHostHas(kv.Value, want)
								}
							}
							if !kept {
								t.Fatal("host-owned exporter view changed")
							}
						}
					})
				}
			}
		}
	}
}
