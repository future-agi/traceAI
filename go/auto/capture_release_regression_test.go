package auto

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

type round4TransportFunc func(*http.Request) (*http.Response, error)

func (f round4TransportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

const round4Request = `{"model":"test","messages":[{"role":"user","content":"hi"}]}`
const round4Response = `{"choices":[{"message":{"role":"assistant","content":"hello"}}]}`

func TestLibraryReconfigureCredentialPrivacy(t *testing.T) {
	for _, sameProcessor := range []bool{false, true} {
		t.Run(map[bool]string{false: "replace-processor", true: "same-processor"}[sameProcessor], func(t *testing.T) {
			ownedRegressionSetup(t)
			app := tracetest.NewSpanRecorder()
			p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(app))
			defer p.Shutdown(context.Background())
			first, last := tracetest.NewSpanRecorder(), tracetest.NewSpanRecorder()
			if sameProcessor {
				last = first
			}
			if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(first), WithCredentials("OLD_FI_API_PRIVATE", "OLD_FI_SECRET_PRIVATE")); err != nil {
				t.Fatal(err)
			}
			ctx, span := p.Tracer("library").Start(context.Background(), "library", trace.WithAttributes(attribute.String("gen_ai.span.kind", "LLM")))
			_, unrelated := p.Tracer("library").Start(context.Background(), "unrelated")
			base := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
				_, _ = io.Copy(io.Discard, r.Body)
				_ = r.Body.Close()
				return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(round4Response))}, nil
			})
			exchange := func(credential string) {
				t.Helper()
				r, _ := http.NewRequestWithContext(ctx, "POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
				r.Header.Set("Authorization", "Bearer "+credential)
				resp, err := Transport(base).RoundTrip(r)
				if err != nil {
					t.Fatal(err)
				}
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
			}
			exchange("PROVIDER_HEADER_PRIVATE")
			for i := 0; i < 2; i++ {
				if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithSpanProcessor(last), WithCredentials("NEW_FI_API_PRIVATE", "NEW_FI_SECRET_PRIVATE")); err != nil {
					t.Fatal(err)
				}
			}
			exchange("AFTER_RECONFIG_PRIVATE")
			text := "PROVIDER_HEADER_PRIVATE AFTER_RECONFIG_PRIVATE OLD_FI_API_PRIVATE OLD_FI_SECRET_PRIVATE"
			span.SetAttributes(attribute.String("output.value", text))
			span.SetStatus(codes.Error, "rejected "+text)
			span.RecordError(errors.New(text))
			span.End()
			unrelated.SetAttributes(attribute.String("output.value", "PROVIDER_HEADER_PRIVATE"))
			unrelated.End()
			if len(first.Ended()) != 2 || !sameProcessor && len(last.Ended()) != 0 || len(app.Ended()) != 2 {
				t.Fatalf("library deduplication/delivery changed: agent=%d app=%d", len(last.Ended()), len(app.Ended()))
			}
			s := first.Ended()[0]
			want := strings.TrimSpace(strings.Repeat("<redacted> ", 4))
			if s.Status().Description != "rejected "+want {
				t.Fatalf("credential echoed through reconfigured status: %q", s.Status().Description)
			}
			for _, kv := range s.Attributes() {
				if kv.Key == "output.value" && kv.Value.AsString() != want {
					t.Fatal("credential echoed through reconfigured output")
				}
			}
			for _, event := range s.Events() {
				for _, kv := range event.Attributes {
					if strings.Contains(kv.Value.Emit(), "PRIVATE") {
						t.Fatal("credential echoed through reconfigured event")
					}
				}
			}
			if !strings.Contains(app.Ended()[0].Status().Description, "PROVIDER_HEADER_PRIVATE") {
				t.Fatal("application processor snapshot mutated")
			}
			for _, kv := range first.Ended()[1].Attributes() {
				if kv.Key == "output.value" && kv.Value.AsString() != "<redacted>" {
					t.Fatal("process-wide request credential redaction missed unrelated span")
				}
			}
			store := agent.current.registration.privacy
			store.mu.Lock()
			remaining := len(store.spans)
			store.mu.Unlock()
			if remaining != 0 {
				t.Fatal("ended library spans retained credential records")
			}
		})
	}
}

func TestNetworkScopePrivacy(t *testing.T) {
	for _, mode := range []string{"scrub", "hide-inputs", "hide-outputs"} {
		t.Run(mode, func(t *testing.T) {
			p := exporterRegressionSetup(t)
			app := tracetest.NewSpanRecorder()
			p.RegisterSpanProcessor(app)
			received := make(chan *collector.ExportTraceServiceRequest, 1)
			sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				b, err := io.ReadAll(r.Body)
				if err != nil {
					t.Error(err)
				}
				x := &collector.ExportTraceServiceRequest{}
				if err := proto.Unmarshal(b, x); err != nil {
					t.Error(err)
				}
				received <- x
				w.Header().Set("Content-Type", "application/x-protobuf")
			}))
			defer sink.Close()
			t.Setenv("FI_BASE_URL", sink.URL)
			cfg := TraceConfig{HideInputs: mode == "hide-inputs", HideOutputs: mode == "hide-outputs"}
			if _, err := InitForTest(context.Background(), WithCredentials("FI_API_PRIVATE", "FI_SECRET_PRIVATE"), WithTraceConfig(cfg)); err != nil {
				t.Fatal(err)
			}
			tracer := p.Tracer("library-FI_API_PRIVATE", trace.WithInstrumentationVersion("FI_SECRET_PRIVATE"), trace.WithSchemaURL("https://schema.example?sig=SCHEMA_PRIVATE"), trace.WithInstrumentationAttributes(
				attribute.String("api_key", "SCOPE_KEY_PRIVATE"),
				attribute.String("input.value", "SCOPE_INPUT_PRIVATE"),
				attribute.String("output.value", "SCOPE_OUTPUT_PRIVATE"),
				attribute.StringSlice("metadata", []string{`{"token":"JSON_PRIVATE","image":"data:image/png;base64,QUJDQUJD"}`, "FI_API_PRIVATE"}),
			))
			_, span := tracer.Start(context.Background(), "library")
			span.End()
			if err := ForceFlush(context.Background()); err != nil {
				t.Fatal(err)
			}
			var request *collector.ExportTraceServiceRequest
			select {
			case request = <-received:
			default:
				t.Fatal("collector received no export after flush")
			}
			if len(request.ResourceSpans) != 1 || len(request.ResourceSpans[0].ScopeSpans) != 1 {
				t.Fatal("missing instrumentation scope")
			}
			ss := request.ResourceSpans[0].ScopeSpans[0]
			if ss.Scope.Name != "library-<redacted>" || ss.Scope.Version != "<redacted>" || strings.Contains(ss.SchemaUrl, "SCHEMA_PRIVATE") {
				t.Fatalf("scope identity leaked: %v", ss)
			}
			attrs := map[string]string{}
			for _, kv := range ss.Scope.Attributes {
				attrs[kv.Key] = kv.Value.GetStringValue()
			}
			if attrs["api_key"] != "<redacted>" || len(ss.Scope.Attributes) != 4 {
				t.Fatalf("scope credential privacy bypassed: %v", attrs)
			}
			if cfg.HideInputs && attrs["input.value"] != RedactedValue || cfg.HideOutputs && attrs["output.value"] != RedactedValue {
				t.Fatalf("scope hide flags bypassed: %v", attrs)
			}
			data, _ := proto.Marshal(request)
			for _, forbidden := range []string{"SCOPE_KEY_PRIVATE", "FI_API_PRIVATE", "FI_SECRET_PRIVATE", "SCHEMA_PRIVATE", "JSON_PRIVATE", "QUJDQUJD"} {
				if strings.Contains(string(data), forbidden) {
					t.Fatalf("actual OTLP export leaked %s", forbidden)
				}
			}
			raw := app.Ended()[0].InstrumentationScope()
			if raw.Name != "library-FI_API_PRIVATE" || raw.Attributes.Len() != 4 {
				t.Fatal("application scope mutated")
			}
			// Custom processors can still call the deprecated alias. It must not
			// provide an escape from the same immutable export view.
			view := exportSpan(app.Ended()[0], agent.current.config, agent.current.res)
			if !reflect.DeepEqual(view.InstrumentationScope(), view.InstrumentationLibrary()) {
				t.Fatal("deprecated scope view bypasses privacy")
			}
		})
	}
}

func TestInvalidCollectorReport(t *testing.T) {
	for _, active := range []bool{false, true} {
		t.Run(map[bool]string{false: "fresh", true: "replacement"}[active], func(t *testing.T) {
			ownedRegressionSetup(t)
			var previous *agentState
			if active {
				if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
					t.Fatal(err)
				}
				previous = agent.current
			}
			for _, url := range []string{"://invalid-endpoint", "https://", "ftp://collector.example", "https://COLLECTOR_USERNAME_PRIVATE:COLLECTOR_PASSWORD_PRIVATE@collector.example", "https://COLLECTOR_USERNAME_PRIVATE:COLLECTOR_PASSWORD_PRIVATE%zz@collector.example", "COLLECTOR_USERNAME_PRIVATE:COLLECTOR_PASSWORD_PRIVATE@collector.example"} {
				t.Setenv("FI_BASE_URL", url)
				_, err := InitForTest(context.Background(), WithCredentials("DUMMY_API_PRIVATE", "DUMMY_SECRET_PRIVATE"))
				if active {
					if err != nil || agent.current != previous || Report().Integrations[0].Status != "instrumented" {
						t.Fatal("second init changed active agent")
					}
					continue
				}
				if err == nil {
					t.Fatalf("invalid URL accepted: %s", url)
				}
				r := Report()
				if strings.Contains(r.Config.BaseURL, "COLLECTOR_USERNAME_PRIVATE") || strings.Contains(r.Config.BaseURL, "COLLECTOR_PASSWORD_PRIVATE") {
					t.Fatal("rejected collector URL leaked userinfo in diagnostics")
				}
				if len(r.Integrations) == 0 || r.Integrations[0].Status != "failed" || r.Integrations[0].Reason != "failed" {
					t.Fatalf("invalid collector reported healthy: %+v", r)
				}
				if agent.current != previous || previous != nil && !previous.active.Load() {
					t.Fatal("failed replacement disturbed previous active agent")
				}
				if url == "://invalid-endpoint" && r.Config.BaseURL != url {
					t.Fatalf("stale attempted URL in diagnostics: %q", r.Config.BaseURL)
				}
			}
		})
	}
}

type trailerAtEOF struct {
	reader  *strings.Reader
	trailer http.Header
}

func (b *trailerAtEOF) Read(p []byte) (int, error) {
	n, err := b.reader.Read(p)
	if err == io.EOF {
		b.trailer.Set("X-Integrity", "computed-at-eof")
	}
	return n, err
}
func (b *trailerAtEOF) Close() error { return nil }

func TestDynamicUploadTrailers(t *testing.T) {
	for _, h2 := range []bool{false, true} {
		t.Run(map[bool]string{false: "http1", true: "http2"}[h2], func(t *testing.T) {
			ownedRegressionSetup(t)
			if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
				t.Fatal(err)
			}
			type result struct {
				body, trailer string
				protocol      int
				err           error
			}
			received := make(chan result, 2)
			srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				b, err := io.ReadAll(r.Body)
				received <- result{string(b), r.Trailer.Get("X-Integrity"), r.ProtoMajor, err}
				w.Header().Set("Content-Type", "application/json")
				_, _ = io.WriteString(w, round4Response)
			}))
			srv.EnableHTTP2 = h2
			srv.StartTLS()
			defer srv.Close()
			base := srv.Client().Transport
			defer base.(*http.Transport).CloseIdleConnections()
			for _, wrapped := range []bool{false, true} {
				transport := base
				if wrapped {
					transport = Transport(base)
				}
				trailers := http.Header{"X-Integrity": nil}
				r, _ := http.NewRequest("POST", srv.URL+"/v1/chat/completions", &trailerAtEOF{strings.NewReader(round4Request), trailers})
				r.Header.Set("Content-Type", "application/json")
				r.Trailer = trailers
				resp, err := (&http.Client{Transport: transport}).Do(r)
				if err != nil {
					t.Fatal(err)
				}
				_, readErr := io.Copy(io.Discard, resp.Body)
				closeErr := resp.Body.Close()
				got := <-received
				wantProtocol := 1
				if h2 {
					wantProtocol = 2
				}
				if got.body != round4Request || got.trailer != "computed-at-eof" || got.protocol != wantProtocol || got.err != nil || readErr != nil || closeErr != nil {
					t.Fatalf("wrapped=%v changed upload: %+v read=%v close=%v", wrapped, got, readErr, closeErr)
				}
			}
		})
	}
}

func TestCompletedResponseReleasesCapture(t *testing.T) {
	for _, mode := range []string{"eof", "length", "close", "read-error"} {
		t.Run(mode, func(t *testing.T) {
			ownedRegressionSetup(t)
			rec := tracetest.NewSpanRecorder()
			if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
				t.Fatal(err)
			}
			var observers []*requestObserver
			base := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
				observers = append(observers, r.Body.(*requestBody).observer)
				_, _ = io.Copy(io.Discard, r.Body)
				_ = r.Body.Close()
				for i := 0; i < 2; i++ {
					b, err := r.GetBody()
					if err != nil {
						t.Fatal(err)
					}
					observers = append(observers, b.(*requestBody).observer)
					_, _ = io.Copy(io.Discard, b)
					_ = b.Close()
				}
				length := int64(-1)
				if mode == "length" {
					length = int64(len(round4Response))
				}
				var body io.ReadCloser = io.NopCloser(strings.NewReader(round4Response))
				if mode == "read-error" {
					body = &round4ReadErrorBody{}
				}
				// Real net/http responses also retain the outgoing request, whose
				// Body and GetBody wrappers must not keep completed upload captures.
				return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: body, ContentLength: length, Request: r}, nil
			})
			payload := `{"model":"test","messages":[{"role":"user","content":"` + strings.Repeat("x", 1024*1024) + `"}]}`
			r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(payload))
			resp, err := Transport(base).RoundTrip(r)
			if err != nil {
				t.Fatal(err)
			}
			for _, observer := range observers {
				if len(observer.capture.data) != len(payload) {
					t.Fatal("test did not retain full request/replay captures")
				}
			}
			b := resp.Body.(*responseBody)
			if b.finalize == nil {
				t.Fatal("test did not exercise response finalizer")
			}
			switch mode {
			case "eof", "read-error":
				_, _ = io.Copy(io.Discard, b)
			case "length":
				_, err = io.ReadFull(b, make([]byte, len(round4Response)))
				if err != nil {
					t.Fatal(err)
				}
			case "close":
				_ = b.Close()
			}
			// Structural reachability assertion, not a timing-dependent finalizer/GC
			// test: the completed response must drop the sole request-capture closure.
			if b.finalize != nil || b.capture.data != nil {
				t.Fatal("completed response retains request/replay closure or response bytes")
			}
			for _, observer := range observers {
				if observer.capture.data != nil || observer.fields != nil || observer.files != nil {
					t.Fatal("completed response request retains upload/replay capture")
				}
			}
			late, err := resp.Request.GetBody()
			if err != nil {
				t.Fatal(err)
			}
			if _, observing := late.(*requestBody); observing {
				t.Fatal("replay after completion recreated upload capture")
			}
			got, err := io.ReadAll(late)
			_ = late.Close()
			if err != nil || string(got) != payload {
				t.Fatal("replay after completion changed application bytes")
			}
			_, _ = io.Copy(io.Discard, b) // Reads after Close cannot repopulate capture.
			_ = b.Close()
			if b.finalize != nil || b.capture.data != nil || len(rec.Ended()) != 1 {
				t.Fatal("completion retained bytes or delivered more than once")
			}
		})
	}
}

func TestCaptureReleasePreservesLateUploadReads(t *testing.T) {
	for _, ct := range []string{"application/json", "multipart/form-data; boundary=b"} {
		t.Run(ct, func(t *testing.T) {
			o := newObserver(ct, "", 4*1024*1024)
			initial := round4Request
			if strings.HasPrefix(ct, "multipart/") {
				initial = "--b\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nmodel\r\n--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\nAUDIO\r\n--b--\r\n"
			}
			o.write([]byte(initial))
			o.finish() // Synchronizes with the multipart parser, without sleeps.
			if strings.HasPrefix(ct, "multipart/") && (len(o.fields) != 1 || len(o.files) != 1) {
				t.Fatal("test did not exercise multipart capture")
			}
			o.release()
			b := &requestBody{ReadCloser: io.NopCloser(strings.NewReader("LATE_UPLOAD_BYTES")), observer: o}
			got, err := io.ReadAll(b)
			closeErr := b.Close()
			if string(got) != "LATE_UPLOAD_BYTES" || err != nil || closeErr != nil {
				t.Fatal("capture retirement changed application upload reads")
			}
			if o.capture.data != nil || o.fields != nil || o.files != nil {
				t.Fatal("late upload recreated completed capture")
			}
		})
	}
}

type round4ReadErrorBody struct{}

func (*round4ReadErrorBody) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }
func (*round4ReadErrorBody) Close() error             { return nil }
