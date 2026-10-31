package auto

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"sync"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/protobuf/proto"
)

func TestS5RegistryBoundLRUAndLongestFirst(t *testing.T) {
	var r secretRegistry
	r.register("short")
	for i := 0; i < secretLimit; i++ {
		r.register(fmt.Sprintf("SYNTHETIC_SECRET_%04d", i))
	}
	old := r.snapshot()
	r.register("SYNTHETIC_SECRET_0000") // Refresh the oldest, without rebuilding.
	if r.snapshot() != old {
		t.Fatal("LRU-only refresh invalidated immutable matcher")
	}
	r.register("SYNTHETIC_EXTRA_SECRET")
	if len(r.values) != secretLimit || r.values["SYNTHETIC_SECRET_0001"] != nil || r.values["SYNTHETIC_SECRET_0000"] == nil || r.values["short"] != nil {
		t.Fatal("registry bound, minimum length or LRU eviction failed")
	}
	if old.replace("SYNTHETIC_SECRET_0001") != "<redacted>" || r.snapshot().replace("SYNTHETIC_SECRET_0001") != "SYNTHETIC_SECRET_0001" {
		t.Fatal("registry snapshots were mutated or stale after eviction")
	}
	var overlap secretRegistry
	for _, value := range []string{"PREFIX_PRIVATE", "PREFIX_PRIVATE_LONGER_SECRET", "ABCDEFGH", "BCDEFGHIJK"} {
		overlap.register(value)
	}
	if got := overlap.snapshot().replace("PREFIX_PRIVATE_LONGER_SECRET ABCDEFGHIJK"); got != "<redacted> A<redacted>" {
		t.Fatal("overlapping secrets were not masked longest first")
	}
}

func TestS5RegistryConcurrentObservationAndExport(t *testing.T) {
	var r secretRegistry
	var wg sync.WaitGroup
	for worker := 0; worker < 8; worker++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for i := 0; i < 40; i++ {
				value := fmt.Sprintf("CONCURRENT_CREDENTIAL_%02d_%04d", worker, i)
				r.register(value)
				if r.snapshot().replace("echo "+value) != "echo <redacted>" {
					t.Error("concurrent snapshot missed a completed registration")
				}
			}
		}(worker)
	}
	wg.Wait()
	if len(r.values) != 320 {
		t.Fatal("concurrent registrations lost values")
	}
}

func TestS5NestedAuthTransportResponseParity(t *testing.T) {
	const headerSecret = "S5_INNER_AUTH_CREDENTIAL_PRIVATE"
	const querySecret = "S5_INNER_QUERY_CREDENTIAL_PRIVATE"
	for _, mode := range []string{"wire", "model-covered", "explicit-suppression"} {
		t.Run(mode, func(t *testing.T) {
			p := exporterRegressionSetup(t)
			app := tracetest.NewSpanRecorder()
			p.RegisterSpanProcessor(app)
			received := privacyCollector(t)
			base := http.DefaultTransport.(*http.Transport).Clone()
			defer base.CloseIdleConnections()
			provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				data, _ := io.ReadAll(r.Body)
				if string(data) != round4Request || r.Header.Get("Authorization") != "Bearer "+headerSecret || r.URL.Query().Get("client_secret") != querySecret {
					t.Error("auth transport request changed")
				}
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Trailer", "X-Final")
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = io.WriteString(w, `{"error":{"message":"provider rejected `+headerSecret+` `+querySecret+`"}}`)
				w.Header().Set("X-Final", "unchanged")
			}))
			defer provider.Close()
			auth := func(inner http.RoundTripper) http.RoundTripper {
				return round4TransportFunc(func(r *http.Request) (*http.Response, error) {
					out := r.Clone(r.Context())
					out.Header.Set("Authorization", "Bearer "+headerSecret)
					out.URL.RawQuery = url.Values{"client_secret": {querySecret}}.Encode()
					return inner.RoundTrip(out)
				})
			}
			type outcome struct {
				status                     int
				body, contentType, trailer string
				readErr, closeErr          error
			}
			exchange := func(ctx context.Context, transport http.RoundTripper) outcome {
				t.Helper()
				req, _ := http.NewRequestWithContext(ctx, "POST", provider.URL+"/v1/chat/completions", strings.NewReader(round4Request))
				resp, err := (&http.Client{Transport: transport}).Do(req)
				if err != nil {
					t.Fatal(err)
				}
				data, readErr := io.ReadAll(resp.Body)
				closeErr := resp.Body.Close()
				return outcome{resp.StatusCode, string(data), resp.Header.Get("Content-Type"), resp.Trailer.Get("X-Final"), readErr, closeErr}
			}
			baseline := exchange(context.Background(), auth(base)) // Agent off.
			if _, err := InitForTest(context.Background(), WithCredentials("S5_FI_API_PRIVATE", "S5_FI_SECRET_PRIVATE")); err != nil {
				t.Fatal(err)
			}
			ctx, parent := p.Tracer("library").Start(context.Background(), "auth-parent")
			if mode == "model-covered" {
				parent.SetAttributes(attribute.String("gen_ai.span.kind", "LLM"))
			}
			nativeCtx, native := p.Tracer("native").Start(ctx, "auth-native")
			_, prestarted := p.Tracer("processing").Start(ctx, "prestarted-child")
			_, unrelated := p.Tracer("processing").Start(context.Background(), "prestarted-unrelated")
			if mode == "explicit-suppression" {
				nativeCtx = SuppressInstrumentation(nativeCtx)
			}
			// The outer capture sees no credentials. The inner default hook must
			// observe the auth clone despite the outer active-capture/S4 marker.
			actual := exchange(nativeCtx, Transport(auth(http.DefaultTransport)))
			if !reflect.DeepEqual(baseline, actual) {
				t.Fatal("agent on/off status, response bytes, headers, trailers or errors differ")
			}
			record := func(span trace.Span) {
				span.SetAttributes(attribute.String("output.value", actual.body))
				span.SetStatus(codes.Error, actual.body)
				span.RecordError(errors.New(actual.body))
			}
			for _, span := range []trace.Span{native, prestarted, unrelated, parent} {
				record(span)
				span.End()
			}
			// Neither ancestor lifetime nor start time restricts registry coverage.
			_, afterRetirement := p.Tracer("processing").Start(ctx, "retired-parent-child")
			record(afterRetirement)
			afterRetirement.End()
			request := flushedPrivacyExport(t, received)
			data, err := proto.Marshal(request)
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(data), headerSecret) || strings.Contains(string(data), querySecret) {
				t.Fatal("nested-auth credential escaped process-wide privacy boundary")
			}
			count, wires := 0, 0
			for _, rs := range request.ResourceSpans {
				for _, ss := range rs.ScopeSpans {
					for _, span := range ss.Spans {
						count++
						for _, kv := range span.Attributes {
							if kv.Key == "traceai.capture.layer" && kv.Value.GetStringValue() == "wire" {
								wires++
							}
						}
					}
				}
			}
			wantWires := 0
			if mode == "wire" {
				wantWires = 1
			}
			if wires != wantWires || count != 5+wantWires || len(app.Ended()) != count {
				t.Fatalf("dedup/delivery changed: wire=%d count=%d app=%d", wires, count, len(app.Ended()))
			}
			if !strings.Contains(app.Ended()[len(app.Ended())-1].Status().Description, headerSecret) {
				t.Fatal("independent exporter snapshot mutated")
			}
		})
	}
}

func TestS5RegistryEveryExportSurface(t *testing.T) {
	const environment = "S5_ENVIRONMENT_CREDENTIAL_PRIVATE"
	const explicit = "S5_REGISTERED_CREDENTIAL_PRIVATE"
	const fiKey = "S5_OPTION_FI_KEY_PRIVATE"
	const fiSecret = "S5_OPTION_FI_SECRET_PRIVATE"
	for _, marker := range []string{"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH"} {
		t.Setenv("TRACEAI_S5_"+marker, environment+marker)
	}
	p := exporterRegressionSetup(t)
	app := tracetest.NewSpanRecorder()
	p.RegisterSpanProcessor(app)
	received := privacyCollector(t)
	if _, err := InitForTest(context.Background(), WithCredentials(fiKey, fiSecret), WithProject("diagnostic-"+explicit, "observe")); err != nil {
		t.Fatal(err)
	}
	all := []string{explicit, fiKey, fiSecret}
	for _, marker := range []string{"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH"} {
		all = append(all, environment+marker)
	}
	text := strings.Join(all, " ")
	attrs := []attribute.KeyValue{
		attribute.String("output.value", text), attribute.StringSlice("array", all),
		attribute.Slice("native", attribute.MapValue(attribute.String("echo", text))),
		attribute.String("json", string(mustJSON(t, map[string]any{"echo": text}))),
		attribute.String("key-"+explicit, text),
	}
	// A resource containing a later-known value must be sanitized at export too.
	res := resource.NewSchemaless(attrs...)
	p2 := sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithSpanProcessor(app))
	defer p2.Shutdown(context.Background())
	p2.RegisterSpanProcessor(agent.current.registration)
	state, _ := trace.ParseTraceState("vendor=" + explicit)
	parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, TraceFlags: trace.FlagsSampled, TraceState: state})
	_, span := p2.Tracer("scope-"+text, trace.WithInstrumentationVersion(text), trace.WithInstrumentationAttributes(attrs...)).Start(trace.ContextWithRemoteSpanContext(context.Background(), parent), "span-"+text, trace.WithAttributes(attrs...), trace.WithLinks(trace.Link{SpanContext: parent, Attributes: attrs}))
	span.SetStatus(codes.Error, text)
	span.AddEvent("event-"+text, trace.WithAttributes(attrs...))
	// Register after start: no inheritance or trace grouping can satisfy this.
	RegisterSecret(explicit)
	span.End()
	data, err := proto.Marshal(flushedPrivacyExport(t, received))
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range all {
		if strings.Contains(string(data), value) {
			t.Fatal("known credential escaped an OTLP export surface")
		}
	}
	view := exportSpan(app.Ended()[0], agent.current.config, agent.current.res)
	if strings.Contains(view.Parent().TraceState().String(), explicit) || strings.Contains(view.Links()[0].SpanContext.TraceState().String(), explicit) || !strings.Contains(app.Ended()[0].Status().Description, explicit) {
		t.Fatal("parent/link state leaked or original span mutated")
	}
	report, err := json.Marshal(Report())
	if err != nil || strings.Contains(string(report), explicit) || strings.Contains(string(report), environment) {
		t.Fatal("registry content appeared in diagnostics")
	}
}

func TestS5ParsedBodyCredentialsReachOtherSpans(t *testing.T) {
	for _, shape := range []string{"json", "multipart"} {
		t.Run(shape, func(t *testing.T) {
			ownedRegressionSetup(t)
			rec := tracetest.NewSpanRecorder()
			if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
				t.Fatal(err)
			}
			const secret = "S5_PARSED_BODY_CREDENTIAL_PRIVATE"
			body, ct := `{"messages":[],"nested":{"client_secret":"`+secret+`"}}`, "application/json"
			if shape == "multipart" {
				body = "--s5\r\nContent-Disposition: form-data; name=\"messages\"\r\n\r\n[]\r\n--s5\r\nContent-Disposition: form-data; name=\"client_secret\"\r\n\r\n" + secret + "\r\n--s5--\r\n"
				ct = "multipart/form-data; boundary=s5"
			}
			_, child := agent.current.provider.Tracer("application").Start(context.Background(), "prestarted-body-echo")
			tr := Transport(round4TransportFunc(func(req *http.Request) (*http.Response, error) {
				data, _ := io.ReadAll(req.Body)
				if string(data) != body {
					t.Error("credential body changed")
				}
				_ = req.Body.Close()
				return &http.Response{StatusCode: 401, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"error":{"message":"` + secret + `"}}`)), ContentLength: -1}, nil
			}))
			req, _ := http.NewRequest("POST", "http://127.0.0.1/v1/chat/completions", strings.NewReader(body))
			req.Header.Set("Content-Type", ct)
			resp, err := tr.RoundTrip(req)
			if err != nil {
				t.Fatal(err)
			}
			data, _ := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			if !strings.Contains(string(data), secret) {
				t.Fatal("response credential echo changed")
			}
			child.SetStatus(codes.Error, string(data))
			child.End()
			if len(rec.Ended()) != 2 {
				t.Fatal("missing body/wire spans")
			}
			for _, span := range rec.Ended() {
				if strings.Contains(span.Status().Description, secret) {
					t.Fatal("parsed credential body echo escaped registry")
				}
			}
		})
	}
}

func TestS5IndexedAndMimeSiblingMediaOTLP(t *testing.T) {
	payload := shortWAV()
	for _, tc := range []struct {
		name  string
		attrs []attribute.KeyValue
		image bool
	}{
		{"indexed-audio", []attribute.KeyValue{attribute.String("input.audio.0", payload)}, false},
		{"indexed-audio-data", []attribute.KeyValue{attribute.String("input.audio.data.0", payload)}, false},
		{"indexed-native-audio", []attribute.KeyValue{attribute.Slice("input.audio", attribute.StringValue(payload))}, false},
		{"flattened-mime-audio", []attribute.KeyValue{attribute.String("gen_ai.input.messages.0.parts.0.inlineData.mimeType", "audio/wav"), attribute.String("gen_ai.input.messages.0.parts.0.inlineData.data", payload)}, false},
		{"flattened-mime-indexed-audio", []attribute.KeyValue{attribute.String("parts.0.inlineData.mimeType", "audio/wav"), attribute.String("parts.0.inlineData.data.0", payload)}, false},
		{"flattened-mime-hidden-image", []attribute.KeyValue{attribute.String("gen_ai.input.messages.0.parts.0.inlineData.mimeType", "image/png"), attribute.String("gen_ai.input.messages.0.parts.0.inlineData.data", payload)}, true},
		{"mime-ancestor-audio", []attribute.KeyValue{attribute.String("parts.0.mimeType", "audio/wav"), attribute.String("parts.0.source.bytes.0", payload)}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			exporterRegressionSetup(t)
			mediaPayload := payload
			if tc.image {
				mediaPayload = smallPNG(t)
				tc.attrs = append([]attribute.KeyValue(nil), tc.attrs...)
				tc.attrs[len(tc.attrs)-1] = attribute.String(string(tc.attrs[len(tc.attrs)-1].Key), mediaPayload)
			}
			attrs := append(append([]attribute.KeyValue(nil), tc.attrs...), attribute.String("parts.1.source.bytes", "SAFE_NEIGHBOR_PAYLOAD"), attribute.String("input.audio.format", "wav"), attribute.String("gen_ai.audio.transcript", "safe transcript"))
			p := sdktrace.NewTracerProvider(sdktrace.WithResource(resource.NewSchemaless(attrs...)))
			defer p.Shutdown(context.Background())
			app := tracetest.NewSpanRecorder()
			p.RegisterSpanProcessor(app)
			received := privacyCollector(t)
			if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("S5_MEDIA_FI_KEY", "S5_MEDIA_FI_SECRET"), WithTraceConfig(TraceConfig{HideInputImages: tc.image})); err != nil {
				t.Fatal(err)
			}
			parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}})
			_, span := p.Tracer("media", trace.WithInstrumentationAttributes(attrs...)).Start(context.Background(), "media", trace.WithAttributes(attrs...), trace.WithLinks(trace.Link{SpanContext: parent, Attributes: attrs}))
			span.AddEvent("media", trace.WithAttributes(attrs...))
			span.End()
			data, err := proto.Marshal(flushedPrivacyExport(t, received))
			if err != nil || strings.Contains(string(data), mediaPayload) || strings.Contains(string(data), mediaPayload[:32]) {
				t.Fatal("indexed/MIME-labelled raw media reached collector")
			}
			if !strings.Contains(string(data), "safe transcript") || !strings.Contains(string(data), "SAFE_NEIGHBOR_PAYLOAD") || !strings.Contains(string(data), "wav") {
				t.Fatal("media classification spread to neighboring parts or safe metadata")
			}
			if !strings.Contains(spanAttr(app.Ended()[0], string(tc.attrs[len(tc.attrs)-1].Key)).Emit(), mediaPayload) {
				t.Fatal("media sanitizer mutated original snapshot")
			}
		})
	}
}
