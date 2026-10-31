package auto

import (
	"bytes"
	"context"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"google.golang.org/protobuf/proto"
)

// R6A verifier BLOCKING B2: every project_*, gen_ai.usage.* and server.* key
// used to be protected, so an application context attribute under those
// prefixes skipped the joint capture-time media scrub and the projection's
// MIME-companion pin. At the default 128 limit (and at 12) headerless PCM then
// reached OTLP after its MIME hint was dropped. The protected set is now the
// spec's closed A7 set (exact identity/provenance keys, numeric usage counters).
func TestR6aProtectedPrefixMediaKeepsContextOTLP(t *testing.T) {
	for _, root := range []string{"project_metadata.media", "gen_ai.usage.extension", "server.media"} {
		for _, single := range []bool{false, true} {
			for _, limit := range []int{12, 32, 127, 128, 129, 256} {
				t.Run(fmt.Sprintf("%s/single=%v/limit=%d", root, single, limit), func(t *testing.T) {
					exporterRegressionSetup(t)
					received := privacyCollector(t)
					limits := sdktrace.NewSpanLimits()
					limits.AttributeCountLimit = limit
					host := tracetest.NewSpanRecorder()
					p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(limits), sdktrace.WithSpanProcessor(host))
					defer p.Shutdown(context.Background())
					if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("R6A_PROTECTED_FI_KEY", "R6A_PROTECTED_FI_SECRET")); err != nil {
						t.Fatal(err)
					}
					extra := []attribute.KeyValue{attribute.String(root+".mime_type", "audio/L16")}
					if single {
						extra = append(extra, attribute.String(root+".data", r6aPCM))
					} else {
						for i := 0; i < 140; i++ {
							extra = append(extra, attribute.String(fmt.Sprintf("%s.data.%03d", root, i), r6aPCM))
						}
					}
					ctx := ContextWithUserID(ContextWithSessionID(context.Background(), "session-1"), "user-1")
					r6aLongChat(t, ContextWithAttributes(ctx, extra...), 80)
					request := flushedPrivacyExport(t, received)
					wire, _ := proto.Marshal(request)
					if bytes.Contains(wire, []byte(r6aPCM)) {
						t.Fatal("raw PCM reached the OTLP export")
					}
					if len(host.Ended()) != 1 {
						t.Fatal("host provider lost the wire span")
					}
					for _, kv := range host.Ended()[0].Attributes() {
						if strings.Contains(kv.Value.Emit(), r6aPCM) {
							t.Fatalf("raw PCM reached the host view through %s", kv.Key)
						}
					}
					attrs := s6ProtoAttrs(request.ResourceSpans[0].ScopeSpans[0].Spans[0])
					if len(attrs) > limit {
						t.Fatalf("export carries %d attributes over the host limit %d", len(attrs), limit)
					}
					for key := range attrs {
						if strings.HasPrefix(key, root+".data") && attrs[root+".mime_type"] == nil {
							t.Fatalf("projection separated %s from its MIME hint", key)
						}
					}
					required := []string{"gen_ai.span.kind", "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.response.id", "http.response.status_code", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens", "traceai.capture.layer", "user.id", "session.id", "output.value", "traceai.wire.attributes_truncated"}
					if limit <= 129 {
						// The 80-message chat's content alone exceeds these limits;
						// at 256 only noncontent metadata is omitted (count stays 0).
						required = append(required, "traceai.capture.dropped_content_count")
					}
					if limit == 256 && single {
						required = required[:len(required)-1] // Everything fits.
					}
					for _, key := range required {
						if limit >= 32 && attrs[key] == nil {
							t.Errorf("limit %d lost %s", limit, key)
						}
					}
				})
			}
		}
	}
}

// The protected set is the closed A7 set; prefixes alone protect nothing.
func TestR6aProtectedSetIsExactAndTyped(t *testing.T) {
	for _, tc := range []struct {
		kv   attribute.KeyValue
		want bool
	}{
		{attribute.String("project_name", "p"), true},
		{attribute.String("project_type", "observe"), true},
		{attribute.String("project_version_name", "v"), true},
		{attribute.String("project_version_id", "id"), true},
		{attribute.String("project_metadata.media.mime_type", "audio/L16"), false},
		{attribute.String("project_metadata.media.data.000", r6aPCM), false},
		{attribute.String("project_", "x"), false},
		{attribute.Int64("gen_ai.usage.input_tokens", 7), true},
		{attribute.Int64("gen_ai.usage.input_tokens.audio", 2), true},
		{attribute.Float64("gen_ai.usage.provider_total_tokens", 12), true},
		{attribute.String("gen_ai.usage.extension.data", r6aPCM), false},
		{attribute.String("gen_ai.usage.extension.mime_type", "audio/L16"), false},
		{attribute.StringSlice("gen_ai.usage.notes", []string{"x"}), false},
		{attribute.String("server.address", "127.0.0.1"), true},
		{attribute.Int("server.port", 443), true},
		{attribute.String("server.media.data", r6aPCM), false},
		{attribute.String("user.id", "u"), true},
		{attribute.Bool("traceai.wire.attributes_truncated", true), true},
	} {
		if got := protectedAttributePriority(tc.kv) >= 0; got != tc.want {
			t.Errorf("%s (%s): protected=%v, want %v", tc.kv.Key, tc.kv.Value.Type(), got, tc.want)
		}
	}
}

// R6A verifier OTHER #3: an ungrouped key whose name equals a content group's
// name ("output" vs output.value/output.mime_type) made group priority depend
// on Go map order, randomly dropping output. Same candidates, same selection.
func TestR6aProjectionGroupNamesDoNotCollide(t *testing.T) {
	base := []attribute.KeyValue{attribute.String("gen_ai.span.kind", "LLM"), attribute.String("traceai.capture.layer", "wire"), attribute.String("gen_ai.provider.name", "openai"), attribute.Int64("gen_ai.usage.total_tokens", 10), attribute.String("output.value", "VISIBLE_OUTPUT"), attribute.String("output.mime_type", "text/plain")}
	for i := 0; i < 80; i++ {
		base = append(base, attribute.String(fmt.Sprintf("gen_ai.input.messages.%d.message.role", i), "user"), attribute.String(fmt.Sprintf("gen_ai.input.messages.%d.message.content", i), fmt.Sprintf("message %d", i)))
	}
	for _, collider := range []attribute.KeyValue{
		attribute.Map("output", attribute.String("format", "annotated")),
		attribute.String("input", "app-input"),
		attribute.String("gen_ai.output", "app-output"),
		attribute.String("gen_ai.parameters", "app-params"),
		attribute.String("gen_ai.input", "app-input"),
	} {
		values := append(append([]attribute.KeyValue(nil), base...), collider)
		var first []attribute.KeyValue
		for i := 0; i < 300; i++ {
			shuffled := append([]attribute.KeyValue(nil), values...)
			for j := range shuffled {
				k := (j*7919 + i*104729) % len(shuffled)
				shuffled[j], shuffled[k] = shuffled[k], shuffled[j]
			}
			got := budgetAttributes(shuffled, 32)
			if first == nil {
				first = got
			} else if !reflect.DeepEqual(got, first) {
				t.Fatalf("%s: projection changed between identical candidate sets", collider.Key)
			}
			kept := map[string]bool{}
			for _, kv := range got {
				kept[string(kv.Key)] = true
			}
			if !kept["output.value"] || !kept["output.mime_type"] {
				t.Fatalf("%s: bounded output lost to a colliding metadata key", collider.Key)
			}
		}
	}
}

// One-line init, real loopback HTTP and OTLP: the colliding native context
// attribute never displaces output, on every one of many identical requests.
func TestR6aGroupCollisionKeepsOutputOTLP(t *testing.T) {
	exporterRegressionSetup(t)
	received := privacyCollector(t)
	limits := sdktrace.NewSpanLimits()
	limits.AttributeCountLimit = 32
	p := sdktrace.NewTracerProvider(sdktrace.WithRawSpanLimits(limits))
	defer p.Shutdown(context.Background())
	if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("R6A_COLLIDE_FI_KEY", "R6A_COLLIDE_FI_SECRET")); err != nil {
		t.Fatal(err)
	}
	ctx := ContextWithAttributes(context.Background(), attribute.Map("output", attribute.String("format", "annotated")))
	for i := 0; i < 40; i++ {
		r6aLongChat(t, ctx, 80)
		request := flushedPrivacyExport(t, received)
		for _, rs := range request.ResourceSpans {
			for _, ss := range rs.ScopeSpans {
				for _, span := range ss.Spans {
					if attrs := s6ProtoAttrs(span); attrs["output.value"] == nil || attrs["output.mime_type"] == nil {
						t.Fatalf("request %d: output dropped behind a colliding context key", i)
					}
				}
			}
		}
	}
}

// Defense in depth: protected identity in context is walked jointly with its
// siblings (a root MIME hint reaches it), yet is never lost when the joint walk
// classifies the whole context object as an image block.
func TestR6aContextIdentityWalkedJointly(t *testing.T) {
	out := captureContext([]attribute.KeyValue{attribute.String("mime_type", "audio/L16"), attribute.String("session.id", r6aPCM), attribute.String("user.id", "user-1")}, nil, Config{}, "chat")
	got := map[string]string{}
	for _, kv := range out {
		got[string(kv.Key)] = kv.Value.Emit()
	}
	if strings.Contains(got["session.id"], r6aPCM) || got["session.id"] == "" {
		t.Fatalf("protected context key bypassed the joint media scrub: %q", got["session.id"])
	}
	if got["user.id"] != "user-1" {
		t.Fatalf("joint walk altered a non-media identity value: %q", got["user.id"])
	}
	cfg := Config{TraceConfig: TraceConfig{HideInputImages: true}}
	out = captureContext([]attribute.KeyValue{attribute.String("type", "image_url"), attribute.String("image_url", "https://example.invalid/a.png"), attribute.String("user.id", "user-1"), attribute.String("session.id", "session-1")}, nil, cfg, "chat")
	got = map[string]string{}
	for _, kv := range out {
		got[string(kv.Key)] = kv.Value.Emit()
	}
	// The image-object fallback keeps the joint walk's form (session.id is
	// catalog credential-marked), never the unchecked original (R6A B2 fallback).
	if got["user.id"] != "user-1" || got["session.id"] != r6aJointSessionID(t) {
		t.Fatalf("protected identity lost or unchecked after the joint walk: %v", got)
	}
	if strings.Contains(fmt.Sprint(got), "example.invalid") {
		t.Fatalf("hidden image reference survived: %v", got)
	}
}
