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

// Even one to three remaining base64 characters can be a truncated catalog
// payload. A URL can reach this state with ordinary attribute limits (28-30),
// not just limits smaller than four. Check the actual agent OTLP destination;
// the host-owned exporter must retain its original view.
func TestRC2fShortMagicPrefixesOTLP(t *testing.T) {
	paths := []string{"app.arbitrary", "input.id.parts.9.arbitrary", "app.transcript.parts.83.arbitrary"}
	for _, owned := range []bool{false, true} {
		for _, prefix := range []string{"", "https://example.invalid/?v="} {
			for _, size := range []int{-1, 1, 2, 3} {
				for _, rep := range []string{"string", "string-array", "generic-slice", "native-map"} {
					t.Run(fmt.Sprintf("owned=%v/query=%v/size=%d/%s", owned, prefix != "", size, rep), func(t *testing.T) {
						media := rc2cWrapped(8)
						if prefix != "" {
							media = prefix + url.QueryEscape(media)
						}
						limit := size
						if limit >= 0 {
							limit += len(prefix)
						}
						tracer, host, received := rc2Destination(t, owned, map[string]int{"length": limit})
						attrs := []attribute.KeyValue{attribute.Int("app.count", 42)}
						for _, path := range paths {
							attrs = append(attrs, rc2cAttr(rep, path, media))
						}
						_, target := tracer.Start(context.Background(), "short-magic-target")
						target.End()
						_, span := tracer.Start(context.Background(), "short-magic", trace.WithLinks(trace.Link{SpanContext: target.SpanContext(), Attributes: attrs}))
						span.SetAttributes(attrs...)
						span.AddEvent("short-magic-event", trace.WithAttributes(attrs...))
						span.End()
						request := flushedPrivacyExport(t, received)
						for _, surface := range []string{"span", "event", "link"} {
							for _, path := range paths {
								value, ok := rc2bAttr(request, "short-magic", surface, path)
								if !ok {
									t.Fatalf("lost %s %s", surface, path)
								}
								rc2AnyValues(value, func(v *commonpb.AnyValue) {
									if s, ok := v.Value.(*commonpb.AnyValue_StringValue); ok && !strings.Contains(s.StringValue, "<omitted:") {
										t.Errorf("raw media prefix at %s %s: %q", surface, path, s.StringValue)
									}
								})
							}
							if count, _ := rc2bAttr(request, "short-magic", surface, "app.count"); count.GetIntValue() != 42 {
								t.Fatal("ordinary numeric metadata changed")
							}
						}
						if host != nil {
							want := media
							if limit >= 0 {
								want = media[:limit]
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
