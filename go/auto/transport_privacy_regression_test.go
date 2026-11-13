package auto_test

// Ports of the independent round-one reproductions. Assertions here describe
// the required behaviour, not the defective observations in the verifier.
import (
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"io"
	"mime/multipart"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func regressionSetup(t *testing.T, config auto.TraceConfig) (*tracetest.SpanRecorder, *sdktrace.TracerProvider) {
	t.Helper()
	return setup(t, auto.WithTraceConfig(config))
}
func regressionExchange(t *testing.T, tr http.RoundTripper, ctx context.Context, endpoint, body string, headers http.Header) {
	t.Helper()
	req, err := http.NewRequestWithContext(ctx, "POST", endpoint, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, vs := range headers {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	resp, err := tr.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
}
func regressionTransport(body, ct string, status int) http.RoundTripper {
	return auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Body != nil {
			_, _ = io.Copy(io.Discard, r.Body)
			_ = r.Body.Close()
		}
		return &http.Response{StatusCode: status, Header: http.Header{"Content-Type": {ct}}, Body: io.NopCloser(strings.NewReader(body))}, nil
	}))
}
func TestCredentialJSON(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	body := `{"model":"m","messages":[],"mcp_servers":[{"authorization_token":"MCP_SECRET"}]}`
	regressionExchange(t, regressionTransport(`{}`, "application/json", 200), context.Background(), "https://api.anthropic.com/v1/messages", body, nil)
	for _, k := range []string{"input.value", "gen_ai.request.parameters"} {
		got, ok := value(wires(rec)[0], k).(string)
		if !ok || strings.Contains(got, "MCP_SECRET") || !strings.Contains(got, "redacted") {
			t.Fatalf("unscrubbed %s=%v", k, got)
		}
	}
}
func TestProviderHeaderScrubbingGaps(t *testing.T) {
	for _, tc := range []struct{ endpoint, body, key, header string }{
		{"https://api.deepgram.com/v1/listen", `{"url":"https://example.com/audio"}`, "Authorization", "Token PROVIDER_SECRET"},
		{"https://api.elevenlabs.io/v1/text-to-speech/voice", `{"text":"hello"}`, "xi-api-key", "PROVIDER_SECRET"},
		{"https://api.openai.com/v1/chat/completions", requestJSON, "api-key", "PROVIDER_SECRET"},
		{"https://api.anthropic.com/v1/messages", `{"messages":[]}`, "anthropic-api-key", "PROVIDER_SECRET"},
		{"https://x.openai.azure.com/v1/chat/completions", requestJSON, "Ocp-Apim-Subscription-Key", "PROVIDER_SECRET"},
	} {
		t.Run(tc.key, func(t *testing.T) {
			rec, _ := regressionSetup(t, auto.TraceConfig{})
			tr := regressionTransport(`{"error":{"message":"Invalid PROVIDER_SECRET"}}`, "application/json", 401)
			regressionExchange(t, tr, context.Background(), tc.endpoint, tc.body, http.Header{tc.key: {tc.header}})
			ss := wires(rec)
			if len(ss) != 1 || ss[0].Status().Description != "Invalid <redacted>" {
				t.Fatalf("error echo not scrubbed: %v", ss)
			}
		})
	}
}
func TestNativeImagePrivacy(t *testing.T) {
	for _, config := range []auto.TraceConfig{{HideInputImages: true}, {Base64ImageMaxLength: 1}} {
		rec, _ := regressionSetup(t, config)
		for _, tc := range []struct{ endpoint, body string }{
			{"https://generativelanguage.googleapis.com/v1/models/g:generateContent", `{"contents":[{"parts":[{"inlineData":{"mimeType":"image/png","data":"PRIVATE_IMAGE"}}]}]}`},
			{"https://api.anthropic.com/v1/messages", `{"messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"PRIVATE_IMAGE"}}]}]}`},
		} {
			regressionExchange(t, regressionTransport(`{}`, "application/json", 200), context.Background(), tc.endpoint, tc.body, nil)
			for _, kv := range wires(rec)[len(wires(rec))-1].Attributes() {
				if strings.Contains(kv.Value.Emit(), "PRIVATE_IMAGE") {
					t.Fatalf("image leaked in %s", kv.Key)
				}
			}
		}
	}
}
func TestHiddenAudioURL(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{HideInputs: true})
	for _, tc := range []struct{ endpoint, body string }{
		{"https://api.assemblyai.com/v2/transcript", `{"audio_url":"https://private.example/recording?sig=SECRET","speech_model":"best"}`},
		{"https://api.deepgram.com/v1/listen", `{"url":"https://private.example/recording?sig=SECRET"}`},
	} {
		regressionExchange(t, regressionTransport(`{}`, "application/json", 200), context.Background(), tc.endpoint, tc.body, nil)
		for _, kv := range wires(rec)[len(wires(rec))-1].Attributes() {
			if strings.Contains(kv.Value.Emit(), "private.example") || strings.Contains(kv.Value.Emit(), "sig=SECRET") {
				t.Fatalf("hidden URL in %s", kv.Key)
			}
		}
	}
}
func TestContextStringSliceSecrets(t *testing.T) {
	t.Setenv("FI_API_KEY", "FI_KEY_SECRET")
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	ctx := auto.ContextWithAttributes(context.Background(), attribute.StringSlice("metadata.credentials", []string{"FI_KEY_SECRET", `{"authorization_token":"PRIVATE"}`, "https://example.com?X-Amz-Signature=PRIVATE"}))
	regressionExchange(t, regressionTransport(responseJSON, "application/json", 200), ctx, "https://api.openai.com/v1/chat/completions", requestJSON, nil)
	// S6 treats the credential-named field structurally, regardless of its type.
	if got := value(wires(rec)[0], "metadata.credentials"); got != "<redacted>" {
		t.Fatalf("credential-named string array was not redacted: %v", got)
	}
}

type blockedReplay struct{ release <-chan struct{} }

func (b blockedReplay) Read([]byte) (int, error) { <-b.release; return 0, io.EOF }
func (b blockedReplay) Close() error             { return nil }
func TestReplayBlocksPastClientDeadline(t *testing.T) {
	regressionSetup(t, auto.TraceConfig{})
	release := make(chan struct{})
	defer close(release)
	var replayed, called atomic.Bool
	client := &http.Client{Timeout: 15 * time.Millisecond, Transport: auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		called.Store(true)
		return &http.Response{StatusCode: 200, Header: http.Header{}, Body: http.NoBody}, nil
	}))}
	r, _ := http.NewRequest("POST", "https://api.deepgram.com/v1/listen", io.NopCloser(strings.NewReader("audio")))
	r.Header.Set("Content-Type", "audio/wav")
	r.GetBody = func() (io.ReadCloser, error) { replayed.Store(true); return blockedReplay{release}, nil }
	done := make(chan error, 1)
	go func() {
		resp, err := client.Do(r)
		if resp != nil {
			_ = resp.Body.Close()
		}
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil || !called.Load() || replayed.Load() {
			t.Fatalf("request delayed/replayed: %v", err)
		}
	case <-time.After(100 * time.Millisecond):
		t.Fatal("replay blocked application past its deadline")
	}
}

type shutdownProbe struct {
	release    <-chan struct{}
	background atomic.Int32
	calls      atomic.Int32
}

func (*shutdownProbe) OnStart(context.Context, sdktrace.ReadWriteSpan) {}
func (*shutdownProbe) OnEnd(sdktrace.ReadOnlySpan)                     {}
func (*shutdownProbe) ForceFlush(context.Context) error                { return nil }
func (p *shutdownProbe) Shutdown(ctx context.Context) error {
	p.calls.Add(1)
	if ctx.Done() == nil {
		p.background.Add(1)
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-p.release:
		return nil
	}
}
func TestShutdownDropsDeadline(t *testing.T) {
	release := make(chan struct{})
	defer close(release)
	probe := &shutdownProbe{release: release}
	_, _ = setup(t, auto.WithSpanProcessor(probe))
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Millisecond)
	defer cancel()
	start := time.Now()
	err := auto.Shutdown(ctx)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 150*time.Millisecond || probe.background.Load() != 0 {
		t.Fatalf("unbounded shutdown: %v %s background=%d", err, time.Since(start), probe.background.Load())
	}
}
func TestTransportFailureMissingNonReplaySpan(t *testing.T) {
	for _, endpoint := range []string{"https://api.openai.com/v1/chat/completions", "https://app.example/v1/chat/completions"} {
		rec, _ := regressionSetup(t, auto.TraceConfig{})
		tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
			_ = r.Body.Close()
			return nil, errors.New("dial failed")
		}))
		req, _ := http.NewRequest("POST", endpoint, io.NopCloser(strings.NewReader(requestJSON)))
		req.Header.Set("Content-Type", "application/json")
		_, err := tr.RoundTrip(req)
		if err == nil {
			t.Fatal("host error lost")
		}
		ss := wires(rec)
		if strings.Contains(endpoint, "app.example") {
			if len(ss) != 0 {
				t.Fatal("unknown unavailable body captured")
			}
			continue
		}
		if len(ss) != 1 || ss[0].Status().Code != codes.Error || value(ss[0], "traceai.wire.body_unavailable") != true || value(ss[0], "input.value") != nil {
			t.Fatalf("known-provider failure lost: %v", ss)
		}
	}
}
func TestMultipartValidationBypass(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	_ = w.WriteField("unrelated_private_form", "TOP_SECRET")
	_ = w.Close()
	r, _ := http.NewRequest("POST", "https://app.example/v1/chat/completions", bytes.NewReader(buf.Bytes()))
	r.Header.Set("Content-Type", w.FormDataContentType())
	resp, err := regressionTransport(`{}`, "application/json", 200).RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if len(wires(rec)) != 0 {
		t.Fatal("unrelated multipart bypassed body validation")
	}
}
func TestReconfigureStopsReusedBatchProcessor(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	proc := sdktrace.NewBatchSpanProcessor(exp, sdktrace.WithBatchTimeout(time.Hour))
	_, p := setup(t, auto.WithSpanProcessor(proc), auto.WithProject("first", "observe"))
	for _, project := range []string{"first", "second", "third"} {
		if _, err := auto.InitForTest(context.Background(), auto.WithProject(project, "observe"), auto.WithSpanProcessor(proc)); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { _ = auto.Shutdown(context.Background()) })
	_, span := p.Tracer("app").Start(context.Background(), "after-reconfigure")
	span.End()
	if err := auto.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := len(exp.GetSpans()); n != 1 {
		t.Fatalf("reused processor exported %d spans, want 1", n)
	}
	_ = auto.Shutdown(context.Background())
	_, span = p.Tracer("app").Start(context.Background(), "app-still-alive")
	if !span.IsRecording() {
		t.Fatal("agent took ownership of app provider")
	}
	span.End()
}
func TestDisableAfterEnableUnsupported(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	cached := regressionTransport(responseJSON, "application/json", 200)
	for i, enabled := range []bool{false, true} {
		if _, err := auto.InitForTest(context.Background(), auto.WithSpanProcessor(rec), auto.WithWireCapture(enabled)); err != nil {
			t.Fatal(err)
		}
		tr := cached
		if enabled {
			tr = auto.Transport(cached)
		}
		regressionExchange(t, tr, context.Background(), "https://api.openai.com/v1/chat/completions", requestJSON, nil)
		want := i + 1 // Later Init never disables or rewires existing capture.
		if len(wires(rec)) != want {
			t.Fatalf("enabled=%v spans=%d", enabled, len(wires(rec)))
		}
	}
}
func TestCompressedTransportPreservesBytes(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	var buf bytes.Buffer
	w := gzip.NewWriter(&buf)
	_, _ = w.Write([]byte(responseJSON))
	_ = w.Close()
	encoded := append([]byte(nil), buf.Bytes()...)
	tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		_, _ = io.Copy(io.Discard, r.Body)
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}, "Content-Encoding": {"gzip"}}, Body: io.NopCloser(bytes.NewReader(encoded))}, nil
	}))
	r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(requestJSON))
	r.Header.Set("Content-Type", "application/json")
	resp, err := tr.RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil || !bytes.Equal(got, encoded) || resp.Header.Get("Content-Encoding") != "gzip" {
		t.Fatal("compressed application exchange changed")
	}
	if len(wires(rec)) != 1 || value(wires(rec)[0], "output.value") != "Hello" || value(wires(rec)[0], "gen_ai.usage.total_tokens") != int64(3) {
		t.Fatal("compressed response not decoded")
	}
}

type scriptedBody struct {
	step    int
	readErr error
	first   string
}

func (b *scriptedBody) Read(p []byte) (int, error) {
	b.step++
	switch b.step {
	case 1:
		return 0, nil
	case 2:
		return copy(p, b.first), nil
	}
	if b.readErr != nil {
		return 0, b.readErr
	}
	return 0, io.EOF
}
func (*scriptedBody) Close() error { return nil }
func TestStreamFailureZeroReadsAndEarlyClose(t *testing.T) {
	for _, mode := range []string{"failure", "early-close", "complete"} {
		t.Run(mode, func(t *testing.T) {
			rec, _ := regressionSetup(t, auto.TraceConfig{})
			body := &scriptedBody{first: "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"}}]}\n\n"}
			if mode == "failure" {
				body.readErr = context.DeadlineExceeded
			}
			tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
				_, _ = io.Copy(io.Discard, r.Body)
				return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: body}, nil
			}))
			r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(requestJSON))
			r.Header.Set("Content-Type", "application/json")
			resp, err := tr.RoundTrip(r)
			if err != nil {
				t.Fatal(err)
			}
			p := make([]byte, 1024)
			if n, err := resp.Body.Read(p); n != 0 || err != nil || len(wires(rec)) != 0 {
				t.Fatal("zero read finalized capture")
			}
			_, _ = resp.Body.Read(p)
			if mode != "early-close" {
				_, _ = resp.Body.Read(p)
			}
			_ = resp.Body.Close()
			_ = resp.Body.Close()
			ss := wires(rec)
			if len(ss) != 1 || value(ss[0], "output.value") != "partial" {
				t.Fatalf("partial output lost: %v", ss)
			}
			if mode == "failure" {
				if ss[0].Status().Code != codes.Error || value(ss[0], "error.type") == nil {
					t.Fatal("failure recorded as OK")
				}
			} else if ss[0].Status().Code != codes.Ok {
				t.Fatal("consumer close marked ERROR")
			}
			if (value(ss[0], "traceai.wire.incomplete") == true) != (mode == "early-close") {
				t.Fatal("incorrect incomplete marker")
			}
		})
	}
}

type closeInterruptedBody struct {
	first    bool
	started  chan struct{}
	closed   chan struct{}
	readDone chan struct{}
}

func (b *closeInterruptedBody) Read(p []byte) (int, error) {
	if !b.first {
		b.first = true
		return copy(p, "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"}}]}\n\n"), nil
	}
	close(b.started)
	<-b.closed
	return 0, io.ErrClosedPipe
}
func (b *closeInterruptedBody) Close() error {
	close(b.closed)
	<-b.readDone // Force the interrupted Read to finalize before Close returns.
	return nil
}
func TestCloseInterruptsReadWithoutTransportError(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	body := &closeInterruptedBody{started: make(chan struct{}), closed: make(chan struct{}), readDone: make(chan struct{})}
	tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		_, _ = io.Copy(io.Discard, r.Body)
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: body}, nil
	}))
	r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(requestJSON))
	r.Header.Set("Content-Type", "application/json")
	resp, err := tr.RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	p := make([]byte, 1024)
	_, _ = resp.Body.Read(p)
	go func() { _, _ = resp.Body.Read(p); close(body.readDone) }()
	<-body.started
	if err = resp.Body.Close(); err != nil {
		t.Fatal(err)
	}
	ss := wires(rec)
	if len(ss) != 1 || ss[0].Status().Code != codes.Ok || value(ss[0], "traceai.wire.incomplete") != true || value(ss[0], "error.type") != nil || value(ss[0], "output.value") != "partial" {
		t.Fatalf("consumer Close misclassified as transport error: %v", ss)
	}
}
